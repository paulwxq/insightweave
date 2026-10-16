"""db 当前态画像的读取、校验与加载提交（Web 发布与 meta_load 共用提交函数）。"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select

from backend.data_sources.repository import get_db_source
from backend.jobs.repository import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    WorkspaceMissingError,
    WorkspaceSourceMismatchError,
    assert_job_publishable,
    assert_workspace_owner,
    get_step_row,
)
from backend.persistence.models import DbProfile
from backend.persistence.platform_db import (
    PlatformSettings,
    load_platform_settings_from_env,
    load_project_env,
    platform_session,
)
from backend.publishing.errors import PublicationStepNotReadyError
from backend.publishing.repository import (
    clear_current,
    insert_publish_row,
    lock_db_source_row,
)
from metaweave.core.clean_targets import resolve_db_output_dir
from metaweave.utils.file_utils import clean_top_level_files
from metaweave.utils.logger import set_current_step
from metaweave.utils.source_id import normalize_source_id

logger = logging.getLogger("backend.profile_loading")

DB_STEP = "db"
_WRITE_SUMMARY = "平台库写入失败"

_COUNT_KEYS = (
    "ordinary_table_count",
    "partitioned_table_count",
    "partition_child_count",
    "view_count",
    "materialized_view_count",
    "object_count",
)


class MetaLoadConfigError(ValueError):
    """参数或工作区归属错误；CLI 将其映射为 exit 2。"""


class MetaLoadSourceNotFoundError(MetaLoadConfigError):
    """指定 source_id 在平台库中不存在。"""


class MetaLoadSourceDisabledError(MetaLoadConfigError):
    """数据源已禁用，不能加载或发布画像。"""


class ProfileInputError(ValueError):
    """database.json 不满足 db_profile 的输入契约。"""


def _require_object(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise ProfileInputError(f"{name} 必须是对象")
    return value


def _require_nonnegative_int(value, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ProfileInputError(f"{name} 必须是非负整数")
    return value


def _require_text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileInputError(f"{name} 必须是非空字符串")
    return value


def _optional_text(value, name: str) -> str | None:
    """缺失或 null → None；否则必须是非空字符串。"""
    if value is None:
        return None
    return _require_text(value, name)


def _profile_fields(document: object, source_id: UUID, job_id: UUID) -> dict:
    """只从已校验产物提取 db_profile 字段，不读取当前源库登记值。"""
    root = _require_object(document, "database.json")
    if type(root.get("schema_version")) is not int or root["schema_version"] != 1:
        raise ProfileInputError("不支持的 database.json schema_version")
    try:
        file_source = normalize_source_id(root.get("source_id"))
    except ValueError:
        raise ProfileInputError("database.json 的 source_id 非法") from None
    if file_source != source_id:
        raise ProfileInputError("database.json 的 source_id 与本次数据源不一致")

    collected_text = _require_text(root.get("collected_at"), "collected_at")
    try:
        collected_at = datetime.fromisoformat(collected_text.replace("Z", "+00:00"))
    except ValueError:
        raise ProfileInputError("collected_at 不是有效时间") from None
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ProfileInputError("collected_at 必须包含时区")

    database = _require_object(root.get("database"), "database")
    db_type = _require_text(database.get("type"), "database.type")
    if db_type != "postgresql":
        raise ProfileInputError("database.type 目前只支持 postgresql")
    host = _require_text(database.get("host"), "database.host")
    db_name = _require_text(database.get("name"), "database.name")
    port = database.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ProfileInputError("database.port 必须是 1–65535 的整数")
    server_version = database.get("server_version")
    if server_version is not None and not isinstance(server_version, str):
        raise ProfileInputError("database.server_version 必须是字符串或 null")
    server_version_full = _optional_text(
        database.get("server_version_full"), "database.server_version_full"
    )
    os_platform = _optional_text(database.get("os_platform"), "database.os_platform")
    size_bytes = _require_nonnegative_int(
        database.get("size_bytes"), "database.size_bytes"
    )
    if database.get("size_scope") != "whole_database":
        raise ProfileInputError("database.size_scope 必须是 whole_database")
    record_count = database.get("record_count")
    if record_count is not None:
        record_count = _require_nonnegative_int(record_count, "database.record_count")

    selected_schemas = root.get("selected_schemas")
    if (
        not isinstance(selected_schemas, list)
        or not selected_schemas
        or any(
            not isinstance(name, str) or not name.strip() for name in selected_schemas
        )
        or len(selected_schemas) != len(set(selected_schemas))
    ):
        raise ProfileInputError("selected_schemas 必须是非空、不重复的名称数组")
    schemas = root.get("schemas")
    if not isinstance(schemas, list) or len(schemas) != len(selected_schemas):
        raise ProfileInputError("schemas 必须与 selected_schemas 一一对应")
    schema_names = []
    summed_counts = dict.fromkeys(_COUNT_KEYS, 0)
    for index, raw_schema in enumerate(schemas):
        schema = _require_object(raw_schema, f"schemas[{index}]")
        schema_names.append(_require_text(schema.get("name"), f"schemas[{index}].name"))
        counts = {
            key: _require_nonnegative_int(schema.get(key), f"schemas[{index}].{key}")
            for key in _COUNT_KEYS
        }
        if counts["object_count"] != sum(
            counts[key] for key in _COUNT_KEYS[:5] if key != "partition_child_count"
        ):
            raise ProfileInputError(f"schemas[{index}].object_count 与分类计数不一致")
        for key in _COUNT_KEYS:
            summed_counts[key] += counts[key]
    if set(schema_names) != set(selected_schemas) or len(schema_names) != len(
        set(schema_names)
    ):
        raise ProfileInputError("schemas 名称与 selected_schemas 不一致")

    totals = _require_object(root.get("totals"), "totals")
    total_counts = {
        key: _require_nonnegative_int(totals.get(key), f"totals.{key}")
        for key in _COUNT_KEYS
    }
    if total_counts != summed_counts:
        raise ProfileInputError("totals 与各 schema 分类计数之和不一致")

    return {
        "source_id": source_id,
        "job_id": job_id,
        "schema_version": 1,
        "collected_at": collected_at,
        "db_type": db_type,
        "host": host,
        "port": port,
        "db_name": db_name,
        "server_version": server_version,
        "server_version_full": server_version_full,
        "os_platform": os_platform,
        "size_bytes": size_bytes,
        "size_scope": "whole_database",
        "record_count": record_count,
        "selected_schemas": selected_schemas,
        "schemas": schemas,
        **total_counts,
    }


def _result(
    started: float,
    success: bool,
    message: str,
    source_id: UUID,
    job_id: UUID,
    publish_id: UUID | None = None,
) -> dict:
    result = {
        "success": success,
        "execution_time": time.perf_counter() - started,
        "message": message,
        "source_id": str(source_id),
        "job_id": str(job_id),
    }
    if publish_id is not None:
        result["publish_id"] = str(publish_id)
    return result


def inspect_db_profile_context(
    session, source_id: UUID, job_id: UUID
) -> tuple[str | None, datetime | None]:
    """检查数据源、工作区归属和 db 步骤状态。不读文件、不写库。

    返回 (最近状态, 运行标识)：运行标识是步骤行 updated_at，start_step/
    finish_step 每次写入都会刷新；调用方在读取输入前快照、提交前核对，
    可发现读取期间该步骤被重跑。
    """
    source = get_db_source(session, source_id)
    if source is None:
        raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
    if not source.is_active:
        raise MetaLoadSourceDisabledError("指定数据源已禁用，不能加载画像")
    assert_workspace_owner(session, job_id, source_id)
    step = get_step_row(session, job_id, "db")
    if step is None:
        return None, None
    return step.last_status, step.updated_at


def resolve_db_profile_input_path(source_id: UUID, job_id: UUID) -> Path:
    """定位 database.json，并确认它是普通、非符号链接文件。"""
    try:
        target_dir = resolve_db_output_dir(source_id, job_id)
        input_path = target_dir / "database.json"
        if input_path.is_symlink() or not input_path.is_file():
            raise ProfileInputError(f"未找到可加载的普通文件: {input_path}")
        return input_path
    except ProfileInputError:
        raise
    except (ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        raise ProfileInputError(message) from None


def read_db_profile_input(source_id: UUID, job_id: UUID) -> tuple[dict, Path]:
    """读取并校验画像；返回字段和 db 目录。不查平台库、不入库。"""
    input_path = resolve_db_profile_input_path(source_id, job_id)
    document = json.loads(input_path.read_text(encoding="utf-8"))
    fields = _profile_fields(document, source_id, job_id)
    return fields, input_path.parent


def replace_db_profile(session, publish_id: UUID, fields: dict) -> DbProfile:
    """整行替换该数据源的当前画像（doc 16 §3.1：主键 source_id，每源一行）。

    实现为事务内 DELETE + INSERT：先删除旧行并 flush，再插入新行。不 flush
    时同主键的 delete+add 会被 unit of work 折叠成只含传入列的 UPDATE，
    fields 未覆盖的列将静默保留旧行的值。
    只改会话不提交；调用方事务回滚则旧行保持不变。
    """
    stmt = select(DbProfile).where(DbProfile.source_id == fields["source_id"])
    existing_rows = list(session.scalars(stmt))
    if len(existing_rows) > 1:
        # 主键为 source_id 时不可能出现；多行说明库仍处于未迁移/未去重的
        # 旧状态（doc 16 §6 部署顺序失误）。替换语义会把它们收敛为一行，
        # 这里只留一条日志证据，不改变行为。
        logger.warning(
            "db_profile 同源存在多行（%d 行），本次替换后收敛为一行",
            len(existing_rows),
        )
    for existing in existing_rows:
        session.delete(existing)
    session.flush()
    row = DbProfile(publish_id=publish_id, **fields)
    session.add(row)
    return row


def commit_db_current_state(
    settings: PlatformSettings,
    source_id: UUID,
    job_id: UUID,
    publish_id: UUID,
    fields: dict,
    expected_run_marker: datetime,
    verify_job: bool = False,
) -> None:
    """成功提交 db 当前态，Web 发布与 meta_load 共用（doc 16 §5）。

    事务：锁源行 → 锁下重查登记（不存在/禁用直接抛错，不写失败记录）
    → 锁下重查步骤状态与运行标识（读取输入后步骤被重跑则拒绝提交）
    → 翻旧 current → 插成功 current 行 → 整行替换画像 → 提交。
    """
    with platform_session(settings) as session:
        source = lock_db_source_row(session, source_id)
        if source is None:
            raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
        if not source.is_active:
            raise MetaLoadSourceDisabledError("指定数据源已禁用，不能加载画像")
        step = get_step_row(session, job_id, DB_STEP)
        if (
            step is None
            or step.last_status != STATUS_SUCCESS
            or step.updated_at != expected_run_marker
        ):
            # start_step 会改回 running 并清空产物目录；读取输入后步骤被
            # 重跑（哪怕已完成）时，本次输入不再代表当前态，拒绝提交。
            raise PublicationStepNotReadyError("db 采集步骤已被重跑")
        if verify_job:
            # Web 发布：锁下再次核对整个 Job（doc 20.1 §11.2）；meta_load 不适用
            assert_job_publishable(session, job_id)
        clear_current(session, source_id, DB_STEP)
        insert_publish_row(
            session,
            publish_id,
            source_id,
            job_id,
            DB_STEP,
            STATUS_SUCCESS,
            is_current=True,
        )
        replace_db_profile(session, publish_id, fields)
        session.commit()


def _record_load_failure(
    settings: PlatformSettings,
    source_id: UUID,
    job_id: UUID,
    publish_id: UUID,
) -> None:
    """独立事务写失败记录：只插 is_current=False 的行，不清 current（doc 16 §1）。

    写入前在源行锁下重查登记（doc 16 §5/§7）：已删除或已禁用时不新增
    失败历史行，直接抛 MetaLoadSourceNotFoundError / MetaLoadSourceDisabledError。
    普通提交错误之后源才被删/禁时，对外报明确的"源不存在/已禁用"而不是
    通用入库失败，与 Web 侧 _store_failure 的锁下重查行为一致。
    """
    try:
        with platform_session(settings) as session:
            source = lock_db_source_row(session, source_id)
            if source is None:
                raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
            if not source.is_active:
                raise MetaLoadSourceDisabledError("指定数据源已禁用，不能加载画像")
            insert_publish_row(
                session,
                publish_id,
                source_id,
                job_id,
                DB_STEP,
                STATUS_FAILED,
                error_summary=_WRITE_SUMMARY,
                is_current=False,
            )
            session.commit()
    except (MetaLoadSourceNotFoundError, MetaLoadSourceDisabledError):
        raise
    except Exception:
        logger.error(
            "失败记录未能提交 source_id=%s job_id=%s publish_id=%s",
            source_id,
            job_id,
            publish_id,
        )


def load_db_profile(source_id, job_id, clean: bool = False) -> dict:
    """加载一个明确指定的 db 画像；clean 仅在提交成功后清理输入目录。"""
    started = time.perf_counter()
    set_current_step("db")
    try:
        source_uuid = normalize_source_id(source_id)
        job_uuid = normalize_source_id(job_id)
    except ValueError:
        raise MetaLoadConfigError("--source-id 和 --job-id 必须是合法 UUID") from None
    load_project_env()
    try:
        settings = load_platform_settings_from_env()
    except ValueError as exc:
        raise MetaLoadConfigError(str(exc)) from None

    try:
        with platform_session(settings) as session:
            try:
                status, run_marker = inspect_db_profile_context(
                    session, source_uuid, job_uuid
                )
            except (WorkspaceMissingError, WorkspaceSourceMismatchError) as exc:
                raise MetaLoadConfigError(str(exc)) from None
            if status != STATUS_SUCCESS:
                return _result(
                    started, False, "db 采集步骤尚未成功", source_uuid, job_uuid
                )
    except MetaLoadConfigError:
        raise
    except Exception as exc:
        logger.error("平台库读取失败（%s）", type(exc).__name__)
        return _result(
            started,
            False,
            f"平台库读取失败（{type(exc).__name__}）",
            source_uuid,
            job_uuid,
        )

    try:
        fields, target_dir = read_db_profile_input(source_uuid, job_uuid)
        input_path = target_dir / "database.json"
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        # 文件和路径异常可以说明原因；不记录原始 JSON 或平台连接参数。
        logger.error("数据库画像输入校验失败（%s）", type(exc).__name__)
        message = (
            str(exc)
            if isinstance(exc, (ProfileInputError, ValueError))
            else type(exc).__name__
        )
        return _result(
            started, False, f"数据库画像输入失败: {message}", source_uuid, job_uuid
        )

    publish_uuid = uuid4()
    try:
        commit_db_current_state(
            settings, source_uuid, job_uuid, publish_uuid, fields, run_marker
        )
    except MetaLoadConfigError:
        # 锁下重查发现源已删除或禁用：不写失败记录，直接按配置错误退出（doc 16 §5）。
        raise
    except PublicationStepNotReadyError:
        # 读取输入后步骤被重跑：不是加载失败，不写 failed 历史行，
        # 让调用方在重跑结束后重新加载。
        logger.error(
            "db 步骤在读取输入后被重跑，取消本次加载"
            " source_id=%s job_id=%s publish_id=%s",
            source_uuid,
            job_uuid,
            publish_uuid,
        )
        return _result(
            started,
            False,
            "db 采集步骤已被重跑，请在结束后重新加载",
            source_uuid,
            job_uuid,
        )
    except Exception as exc:
        logger.error("数据库画像入库失败（%s）", type(exc).__name__)
        _record_load_failure(settings, source_uuid, job_uuid, publish_uuid)
        return _result(
            started,
            False,
            f"数据库画像入库失败（{type(exc).__name__}）",
            source_uuid,
            job_uuid,
        )

    if clean:
        try:
            current_target = resolve_db_output_dir(source_uuid, job_uuid)
            if current_target != target_dir:
                raise ValueError("清理目标与读取目标不一致")
            logger.warning(
                "meta_load --clean 清理目标: %s；范围：删除顶层所有文件，保留子目录及其内容",
                current_target,
            )
            clean_top_level_files(current_target)
        except Exception as exc:
            logger.error("画像已入库，但目录清理失败（%s）", type(exc).__name__)
            return _result(
                started,
                False,
                "画像已入库、清理失败，请检查目录和日志",
                source_uuid,
                job_uuid,
                publish_uuid,
            )

    logger.info("数据库画像已入库: publish_id=%s，文件=%s", publish_uuid, input_path)
    return _result(
        started,
        True,
        f"数据库画像加载完成；文件: {input_path}；清理: {'已完成' if clean else '未执行'}",
        source_uuid,
        job_uuid,
        publish_uuid,
    )
