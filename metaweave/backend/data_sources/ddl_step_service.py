"""受管 DDL 步骤的入口准备链；独立 CLI 与 Web 采集共用（doc 16 §4）。

错误类型复用 `DbStepConfigError` / `SourceDisabledError`（语义同为"采集
准备链错误"），CLI 与 API 沿用 db 步骤的现有映射，不另建一套。
"""

import copy
import json
import logging
import time
from pathlib import Path
from uuid import uuid4

from backend.data_sources.db_step_service import (
    DbStepConfigError,
    SourceDisabledError,
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
    start_step,
)
from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    platform_session,
)
from backend.security.source_password import decrypt_password
from metaweave.core.clean_targets import resolve_ddl_output_dir
from metaweave.core.metadata.db_step import validate_schema_names
from metaweave.core.metadata.execution import (
    load_main_config_snapshot,
    set_operation_step,
)
from metaweave.core.metadata.generator import (
    MetadataGenerator,
    validate_exclude_tables,
)
from metaweave.core.metadata.generation_config import MetadataGenerationConfig
from metaweave.utils.file_utils import clean_top_level_files
from metaweave.utils.source_id import normalize_source_id

logger = logging.getLogger("backend.data_sources")

DDL_STEP_NAME = "ddl"
_EMPTY_SCOPE_MESSAGE = "选中范围内没有可采集对象"
# 采集成功时写入受管 DDL 目录的文件清单（doc 16 §4/§5）：发布读取前
# 据此核对缺件，防止目录缺文件被当作完整集合发布。与发布侧
# backend.profile_loading.ddl_loader 的同名常量必须一致（测试锁定）。
DDL_MANIFEST_NAME = "manifest.json"


def _failure(started: float, message: str, job_id=None) -> dict:
    result = {
        "success": False,
        "execution_time": time.perf_counter() - started,
        "message": message,
    }
    if job_id is not None:
        result["job_id"] = str(job_id)
    return result


def build_managed_output_snapshot(
    main_config: dict, source_id, job_uuid, db_name: str, selected_schemas
) -> dict:
    """无凭据的公共工作区快照（doc 19.1 §3）：DDL/JSON 与 MD 共用。

    深拷贝主配置后以登记库身份（`db_name`/`selected_schemas`）覆盖
    `database.database`/`database.schemas`，输出六键全部指向同一作业
    工作区（各经受管 resolver 校验；`cql_directory` 为 doc 23.2 §5.1
    第 6 键，rel 发布生成 cql 使用——生成器读 `output.cql_directory`）；
    连接字段显式置 ``None``，不保留
    主配置中的全局连接值——MD 等文件步骤不解密密码也不连源库，误用
    时由 `DatabaseConnector` 在建连前拒绝。
    """
    ddl_dir = resolve_ddl_output_dir(source_id, job_uuid)
    workspace_root = ddl_dir.parent
    snapshot = copy.deepcopy(main_config)
    # 非对象节点在归一化前拒绝（doc 19.1 §8 验收 2）：dict(5) 会以
    # TypeError 逃逸成 503，dict(["ab"]) 会被静默改写成 {"a": "b"}；
    # 缺省与 null 视为空对象。DDL/JSON/MD 三链共用本函数，一处覆盖。
    database_node = snapshot.get("database")
    if database_node is not None and not isinstance(database_node, dict):
        raise ValueError("database 必须是对象")
    output_node = snapshot.get("output")
    if output_node is not None and not isinstance(output_node, dict):
        raise ValueError("output 必须是对象")
    database = dict(database_node or {})
    database.update(
        {
            "database": db_name,
            "schemas": list(selected_schemas),
            "host": None,
            "port": None,
            "user": None,
            "password": None,
            "sslmode": None,
        }
    )
    snapshot["database"] = database
    output = dict(output_node or {})
    output.update(
        {
            "output_dir": str(workspace_root),
            "ddl_directory": str(ddl_dir),
            "markdown_directory": str(workspace_root / "md"),
            "json_directory": str(workspace_root / "json"),
            # doc 23.1 §5.2：rel 的 writer 读 output.rel_directory；其余步骤
            # 不消费该键（ddl/json/md/domain/dim_config 行为不变）
            "rel_directory": str(workspace_root / "rel"),
            # doc 23.2 §5.1：rel 发布的 CQLGenerator 读 output.cql_directory；
            # 采集步骤不消费该键，cql 产物目录由发布过程创建
            "cql_directory": str(workspace_root / "cql"),
        }
    )
    snapshot["output"] = output
    return snapshot


def build_managed_ddl_config(main_config: dict, source, password, job_uuid) -> dict:
    """受管 DDL 的内存配置快照（doc 16 §4）：深拷贝后覆盖，不落盘。

    在公共工作区快照（doc 19.1 §3）之上追加登记源连接参数：host、port、
    库名、用户、解密密码与 `sslmode`（显式覆盖含 None，不继承主配置，
    doc 18.1 §3.2）。DDL/JSON 调用方继续使用本函数。
    """
    snapshot = build_managed_output_snapshot(
        main_config,
        source.source_id,
        job_uuid,
        source.db_name,
        source.selected_schemas,
    )
    snapshot["database"].update(
        {
            "host": source.host,
            "port": source.port,
            "user": source.username,
            "password": password,
            "sslmode": source.sslmode,
        }
    )
    return snapshot


def verify_ddl_collection(result, ddl_dir: Path) -> tuple[bool, str]:
    """核对受管 DDL 成功条件（doc 16 §4）。

    `result.success` 为真、无失败对象、处理对象数大于零、按规范路径去重
    后的 `.sql` 输出数与成功对象数相等，全部输出位于本次受管 DDL 目录内，
    每个声称的输出都是磁盘上真实存在的普通文件，且目录中的实际 `.sql`
    文件与输出清单完全一致；任一不满足都判失败。零对象有意失败，避免
    一次空集发布删除已有当前 DDL。
    """
    if not getattr(result, "success", False):
        return False, "DDL 生成未全部成功"
    if result.failed_tables:
        return False, f"有 {result.failed_tables} 个对象采集失败"
    if result.processed_tables <= 0:
        return False, _EMPTY_SCOPE_MESSAGE
    sql_outputs = [
        Path(path) for path in result.output_files if str(path).endswith(".sql")
    ]
    # 相同路径去重后只算一份：两个对象落到同一文件时磁盘上只有一份，
    # 不能用重复路径冒充"实际输出数"（doc 16 §4 以实际输出为准）。
    declared = {path.resolve() for path in sql_outputs}
    if len(declared) != result.processed_tables:
        return False, "DDL 输出文件数与成功对象数不一致"
    for path in (Path(item) for item in result.output_files):
        if path.resolve().parent != ddl_dir:
            return False, "存在不在本次受管 DDL 目录内的输出文件"
        if not path.is_file() or path.is_symlink():
            # 生成器只上报路径（save_text 失败只返回 False），以磁盘为准
            return False, "存在声称成功但未实际落盘的输出文件"
    # 发布读取时直接 glob 本目录：混入清单之外的 .sql 会被一并发布出去，
    # 这里按集合相等核对，目录实际内容必须与清单完全一致。
    actual = {path.resolve() for path in ddl_dir.glob("*.sql")}
    if actual != declared:
        return False, "受管 DDL 目录的实际输出文件与清单不一致"
    return True, ""


def _write_ddl_manifest(ddl_dir: Path, result) -> None:
    """采集成功后把本次完整文件清单写进受管 DDL 目录（doc 16 §4）。

    目录只反映"现存"文件；采集后被删掉一份时，发布若无清单凭据会把
    剩余文件当作完整集合发布，无提示缩小当前表定义。清单与产物同目录
    （非 `.sql` 后缀不进发布 glob），每次采集随目录清理后重写。
    """
    names = sorted(
        {Path(path).name for path in result.output_files if str(path).endswith(".sql")}
    )
    (ddl_dir / DDL_MANIFEST_NAME).write_text(
        json.dumps({"files": names}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_ddl_step(config_path, source_id, job_id=None, max_workers=None) -> dict:
    """准备链：校验 → 登记作业状态 → 清理本次 DDL 目录 → 生成 → 核对 → 写回状态。

    配置错误（参数、主配置、数据源、--job-id 归属、目录边界）抛
    `DbStepConfigError`（exit 2）；平台库写入失败与采集失败返回
    success=False（exit 1）。
    """
    started = time.perf_counter()
    set_operation_step(DDL_STEP_NAME)
    if isinstance(max_workers, bool) or not isinstance(max_workers, (int, type(None))):
        raise DbStepConfigError(f"max_workers 必须为正整数（检测到: {max_workers!r}）")
    if max_workers is not None and max_workers < 1:
        raise DbStepConfigError(f"max_workers 必须为正整数（检测到: {max_workers!r}）")
    try:
        source_uuid = normalize_source_id(source_id)
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    job_uuid = None
    if job_id is not None:
        try:
            job_uuid = normalize_source_id(job_id)
        except ValueError:
            raise DbStepConfigError("--job-id 不是合法 UUID") from None
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

    if job_uuid is None:
        job_uuid = uuid4()

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
            if job_id is not None:
                try:
                    assert_workspace_owner(session, job_uuid, source_uuid)
                except (WorkspaceMissingError, WorkspaceSourceMismatchError) as exc:
                    raise DbStepConfigError(str(exc)) from None
            try:
                validate_schema_names(source.selected_schemas)
                password = decrypt_password(source.password_ciphertext)
            except ValueError as exc:
                raise DbStepConfigError(str(exc)) from None
            snapshot = build_managed_ddl_config(main_config, source, password, job_uuid)
            ddl_dir = Path(snapshot["output"]["ddl_directory"])
            # 非法生成配置在登记作业前失败（参数类错误走 exit 2，不写 job_info）；
            # exclude_tables 类型检查与 JSON/MD 准备链同款（doc 19.1 §4 三链一致）
            MetadataGenerationConfig.from_config(snapshot)
            validate_exclude_tables(snapshot.get("database") or {})
    except DbStepConfigError:
        raise
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:
        return _failure(started, f"平台库连接或读取失败（{type(exc).__name__}）")

    try:
        with platform_session(settings) as session:
            start_step(session, job_uuid, source_uuid, DDL_STEP_NAME)
    except SourceRegistrationMissingError:
        # 锁下重查发现登记已被并发删除（doc 16 §6）：不写 running、不新增步骤行
        raise DbStepConfigError("未找到指定 source_id 的数据源") from None
    except StepAlreadyRunningError as exc:
        return _failure(started, str(exc), job_uuid)
    except Exception as exc:
        return _failure(started, f"作业状态登记失败（{type(exc).__name__}）", job_uuid)

    # 受管模式固定在启动后清理本次 DDL 目录（doc 16 §4：每次生成完整集合，
    # 不保留上次已不存在的对象文件）；目录已由 resolver 校验边界。
    try:
        clean_top_level_files(ddl_dir)
        result = MetadataGenerator(config_path, config=snapshot).generate(
            step=DDL_STEP_NAME,
            max_workers=max_workers if max_workers is not None else 4,
        )
    except Exception as exc:
        # 消息只写错误类别；生成器内部日志已记录细节，不回显异常原文
        message = f"DDL 采集失败（{type(exc).__name__}）"
        try:
            with platform_session(settings) as session:
                finish_step(session, job_uuid, DDL_STEP_NAME, STATUS_FAILED)
        except Exception as state_exc:
            message += (
                f"；作业状态更新失败（{type(state_exc).__name__}），"
                "请查看日志确认作业状态"
            )
        return _failure(started, message, job_uuid)

    ok, reason = verify_ddl_collection(result, ddl_dir)
    if ok:
        try:
            _write_ddl_manifest(ddl_dir, result)
        except OSError:
            # 清单写不进去，本次产物将因缺少清单而不可发布，不能标 success
            ok = False
            reason = "DDL 采集清单写入失败"
    try:
        with platform_session(settings) as session:
            finish_step(
                session,
                job_uuid,
                DDL_STEP_NAME,
                STATUS_SUCCESS if ok else STATUS_FAILED,
            )
    except Exception as exc:
        # 产物目录只进日志，API 回显仅 job_id（受控路径不出错误消息）。
        logger.error(
            "作业状态更新失败（%s） source_id=%s job_id=%s 产物目录=%s",
            type(exc).__name__,
            source.source_id,
            job_uuid,
            ddl_dir,
        )
        return _failure(
            started,
            "采集产物可能已生成，但作业状态更新失败"
            f"（{type(exc).__name__}）；job_id={job_uuid}，请查看日志确认作业状态与产物",
            job_uuid,
        )

    if not ok:
        return _failure(started, reason, job_uuid)
    return {
        "success": True,
        "execution_time": time.perf_counter() - started,
        "message": f"受管 DDL 采集完成（{result.processed_tables} 个对象）",
        "job_id": str(job_uuid),
    }
