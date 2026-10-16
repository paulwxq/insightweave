"""受管 MD 步骤的入口准备链；Web 采集与重跑共用（doc 19.1 §3/§4/§5）。

MD 只消费同一作业已成功的 JSON 产物：不解密源库密码、不建立源库连接；
使用无凭据工作区快照。输出核验以同一 `OutputFormatter.generate_markdown()`
对缓存限定 JSON 文档重渲染并逐字节比对（doc 19.1 §5），成功清单记录
JSON 运行标识 `source_json_updated_at`。
"""

import io
import logging
import time
from datetime import datetime
from pathlib import Path

from backend.data_sources.db_step_service import (
    DbStepConfigError,
    SourceDisabledError,
)
from backend.data_sources.ddl_step_service import build_managed_output_snapshot
from backend.data_sources.json_step_service import (
    ManagedJsonInputError,
    read_managed_json_input,
)
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
from metaweave.utils.time_utils import utc_isoformat
from metaweave.core.clean_targets import resolve_md_output_dir
from metaweave.core.metadata.db_step import validate_schema_names
from metaweave.core.metadata.execution import (
    load_main_config_snapshot,
    set_operation_step,
)
from metaweave.core.metadata.formatter import validate_output_numeric_options
from metaweave.core.metadata.generator import (
    MetadataGenerator,
    table_matches_exclusion,
    validate_exclude_tables,
)
from metaweave.core.metadata.generation_config import MetadataGenerationConfig
from metaweave.core.metadata.json_loader import parse_strict_stem
from metaweave.core.metadata.models import normalize_database_object_types
from metaweave.utils.file_utils import atomic_write_json, clean_top_level_files
from metaweave.utils.source_id import normalize_source_id

logger = logging.getLogger("backend.data_sources")

MD_STEP_NAME = "md"
JSON_STEP_FOR_MD = "json"
MD_MANIFEST_NAME = "manifest.json"
_SCOPE_MISMATCH_TEMPLATE = (
    "现有 JSON 画像与当前采集范围不一致；请按当前范围重跑 DDL、JSON 后再运行 {step}"
)


def _failure(started: float, message: str, job_id=None) -> dict:
    result = {
        "success": False,
        "execution_time": time.perf_counter() - started,
        "message": message,
    }
    if job_id is not None:
        result["job_id"] = str(job_id)
    return result


def _write_mode_bytes(text: str) -> bytes:
    """按写盘同源的规则编码期望文本（doc 19.1 §5）。

    ``save_text()`` 是文本模式写盘，``'\n'`` 会被翻译为 ``os.linesep``
    （Windows 为 CRLF）；重渲染期望必须走同一翻译，否则跨平台核验会
    全量失败。用 ``TextIOWrapper(newline=None)`` 与写盘机制完全同源。
    """
    buffer = io.BytesIO()
    wrapper = io.TextIOWrapper(buffer, encoding="utf-8", newline=None)
    wrapper.write(text)
    wrapper.flush()
    return buffer.getvalue()


def _marker_iso(marker: datetime) -> str:
    """JSON 运行标识序列化为 UTC 六位微秒、Z 结尾（doc 19.3 §3）。"""
    return utc_isoformat(marker)


def _precheck_generator_config(snapshot: dict) -> None:
    """清理前覆盖生成器构造期解析（doc 19.1 §4.5，无文件/数据库副作用）。

    复用真实构造（`MetadataProfiler` / `LogicalKeyDetector`），节点类型
    检查先于构造；异常统一抛带配置路径的 ``ValueError``，由调用方转
    ``DbStepConfigError``，不让 ``AttributeError`` 冒充平台库故障。
    """
    from metaweave.core.metadata.logical_key_detector import LogicalKeyDetector
    from metaweave.core.metadata.profiler import MetadataProfiler

    sampling = snapshot.get("sampling", {})
    if not isinstance(sampling, dict):
        raise ValueError("sampling 必须是对象")
    # 生成器构造期 `.get()` 而 Profiler 不读的子节点须显式检查
    # （如 column_statistics 为 null 时 Profiler 不炸、生成器构造才炸，
    # 预检漏过会让清理先于失败发生）
    if "column_statistics" in sampling and not isinstance(
        sampling["column_statistics"], dict
    ):
        raise ValueError("sampling.column_statistics 必须是对象")
    logical_key = snapshot.get("logical_key_detection", {})
    if not isinstance(logical_key, dict):
        raise ValueError("logical_key_detection 必须是对象")
    # 已知构造期配置解析异常统一收敛为带类别的 ValueError（doc 19.1
    # §4.5）：AttributeError（null 子节点）与 re.error（非法正则，非
    # ValueError 子类）不得漏出冒充平台库故障
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
    """范围预检（doc 19.1 §4.3）：schema/对象类型/排除规则与生成器同规则。

    doc 23.1 §5.2（N4）公开化并增加 ``step_label``：文案按步格式化，md 传
    缺省 ``"MD"`` 与历史文案逐字节一致；rel 传 ``"Rel"``。
    """
    database = snapshot.get("database") or {}
    # 缺省键沿用生成器默认 [table]；显式 null/非法由 normalize 受控拒绝
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
    """核对受管 MD 成功条件（doc 19.1 §5）。

    声明路径与对象一一映射、目录全集一致、stem 集合与 JSON 清单一致；
    再以同一格式化器对缓存限定 JSON 文档重渲染，与实际文件逐字节比对。
    """
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
    # glob 侧同样先查类型再 resolve（doc 19.1 §3）：循环符号链接会让
    # resolve() 抛 RuntimeError，指向已声明文件的链接会被静默并入。
    actual = set()
    for path in md_dir.glob("*.md"):
        if path.is_symlink() or not path.is_file():
            return False, f"受管 MD 目录存在非常规文件: {path.name}"
        actual.add(path.resolve())
    if actual != declared:
        return False, "受管 MD 目录的实际输出文件与声明清单不一致"

    # 声明路径 -> 对象键一对一映射（doc 19.1 §5：不用命名模板另拼期望路径）
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


def _json_still_unchanged(session, job_uuid, marker) -> bool:
    """锁下复查：JSON 仍为 success 且运行标识未变（doc 19.1 §4.4）。"""
    json_step = get_step_row(session, job_uuid, JSON_STEP_FOR_MD)
    return (
        json_step is not None
        and json_step.last_status == STATUS_SUCCESS
        and json_step.updated_at == marker
    )


def run_md_step(config_path, source_id, job_id=None) -> dict:
    """准备链：校验 → 锁下核对 JSON 并登记 running → 清理 → 生成 →
    核验 → 锁下复查 JSON、写清单并提交状态（doc 19.1 §3/§4/§5）。"""
    started = time.perf_counter()
    set_operation_step(MD_STEP_NAME)
    if job_id is None:
        # 新作业 MD 单选：没有任何工作区可用，不伪造 job_id（doc 19.1 §3）
        raise DbStepConfigError(
            "同一作业的 JSON 产物未就绪；请先执行 JSON 或指定已有 job_id"
        )
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

    # ---- 准备链：登记源、JSON 前置、无凭据快照与配置预检 ----
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
            json_step = get_step_row(session, job_uuid, JSON_STEP_FOR_MD)
            if json_step is None or json_step.last_status != STATUS_SUCCESS:
                raise DbStepConfigError(
                    "同一作业的 JSON 产物未就绪；请先执行 JSON 或指定已有 job_id"
                )
            json_marker = json_step.updated_at
            validate_schema_names(source.selected_schemas)
            try:
                objects = read_managed_json_input(source_uuid, job_uuid, source.db_name)
            except ManagedJsonInputError as exc:
                raise DbStepConfigError(f"同一作业的 JSON 产物未就绪：{exc}") from None
            snapshot = build_managed_output_snapshot(
                main_config,
                source_uuid,
                job_uuid,
                source.db_name,
                source.selected_schemas,
            )
            md_dir = resolve_md_output_dir(source_uuid, job_uuid)
            if Path(snapshot["output"]["markdown_directory"]) != md_dir:
                raise DbStepConfigError("受管 MD 输出目录与作业工作区不一致")
            check_objects_in_scope(objects, snapshot, source)
            _precheck_generator_config(snapshot)
    except DbStepConfigError:
        # 含 SourceDisabledError 子类（有独立 API 映射），准备链错误一律放行
        raise
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:
        return _failure(started, f"平台库连接或读取失败（{type(exc).__name__}）")

    # ---- 锁下核对 JSON 并登记 running（同一事务，start_step 的 commit 释放锁）----
    try:
        with platform_session(settings) as session:
            if lock_db_source_row(session, source_uuid) is None:
                raise SourceRegistrationMissingError(source_uuid)
            if not _json_still_unchanged(session, job_uuid, json_marker):
                raise DbStepConfigError(
                    "同一作业的 JSON 产物未就绪；请先执行 JSON 或指定已有 job_id"
                )
            start_step(session, job_uuid, source_uuid, MD_STEP_NAME)
    except SourceRegistrationMissingError:
        raise DbStepConfigError("未找到指定 source_id 的数据源") from None
    except DbStepConfigError:
        raise
    except StepAlreadyRunningError as exc:
        return _failure(started, str(exc), job_uuid)
    except Exception as exc:
        return _failure(started, f"作业状态登记失败（{type(exc).__name__}）", job_uuid)

    # ---- 清理本次 MD 目录并生成（复用既有 md 生成链，不连源库）----
    generator = None
    try:
        clean_top_level_files(md_dir)
        generator = MetadataGenerator(config_path, config=snapshot)
        result = generator.generate(step=MD_STEP_NAME, max_workers=4)
    except Exception as exc:
        message = f"MD 生成失败（{type(exc).__name__}）"
        try:
            with platform_session(settings) as session:
                finish_step(session, job_uuid, MD_STEP_NAME, STATUS_FAILED)
        except Exception as state_exc:
            message += (
                f"；作业状态更新失败（{type(state_exc).__name__}），"
                "请查看日志确认作业状态"
            )
        return _failure(started, message, job_uuid)

    # ---- 核验（异常也按失败处理并写回状态，绝不把步骤留在 running）----
    try:
        ok, reason = verify_md_result(
            result, md_dir, objects, generator, source.db_name
        )
    except Exception as exc:
        ok, reason = False, f"MD 采集核验失败（{type(exc).__name__}）"

    # ---- 锁下复查 JSON、写清单并提交状态（同一事务，finish 的 commit 释放锁）----
    try:
        with platform_session(settings) as session:
            if lock_db_source_row(session, source_uuid) is None:
                raise SourceRegistrationMissingError(source_uuid)
            json_unchanged = _json_still_unchanged(session, job_uuid, json_marker)
            if ok and json_unchanged:
                try:
                    names = sorted(path.name for path in md_dir.glob("*.md"))
                    atomic_write_json(
                        {
                            "files": names,
                            "source_json_updated_at": _marker_iso(json_marker),
                        },
                        md_dir / MD_MANIFEST_NAME,
                    )
                except OSError:
                    # 写盘失败不污染数据库事务：同事务落定 failed（doc 19.1 §5）
                    ok = False
                    reason = "MD 采集清单写入失败"
            status = STATUS_SUCCESS if (ok and json_unchanged) else STATUS_FAILED
            if ok and not json_unchanged:
                reason = (
                    "同一作业的 JSON 已重跑，本次 MD 产物已失效；"
                    "请重跑 JSON 后再运行 MD"
                )
            finish_step(session, job_uuid, MD_STEP_NAME, status)
    except SourceRegistrationMissingError:
        _discard_manifest(md_dir)
        return _failure(
            started,
            "MD 产物可能已生成，但数据源登记已不存在；"
            f"job_id={job_uuid}，请查看日志确认作业状态",
            job_uuid,
        )
    except Exception as exc:
        _discard_manifest(md_dir)
        logger.error(
            "作业状态更新失败（%s） source_id=%s job_id=%s 产物目录=%s",
            type(exc).__name__,
            source_uuid,
            job_uuid,
            md_dir,
        )
        return _failure(
            started,
            "MD 产物可能已生成，但作业状态更新失败"
            f"（{type(exc).__name__}）；job_id={job_uuid}，请查看日志确认作业状态与产物",
            job_uuid,
        )

    if not ok or not json_unchanged:
        _discard_manifest(md_dir)
        return _failure(started, reason, job_uuid)
    return {
        "success": True,
        "execution_time": time.perf_counter() - started,
        "message": f"受管 MD 生成完成（{result.processed_tables} 个对象）",
        "job_id": str(job_uuid),
    }


def _discard_manifest(md_dir: Path) -> None:
    """状态未提交成功时移除新清单（doc 19.1 §5：不做跨资源原子事务）。"""
    manifest = md_dir / MD_MANIFEST_NAME
    try:
        manifest.unlink(missing_ok=True)
    except OSError:
        logger.warning("MD 清单删除失败 (%s)", manifest.name)
