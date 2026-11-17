"""受管 DDL 步骤 worker（doc 16 §4、6.2 §7.1）。

保留公共工作区快照（`build_managed_output_snapshot`）与凭据版
（`build_managed_ddl_config`）供 JSON/MD/发布链复用；错误类型沿用
`DbStepConfigError` / `SourceDisabledError`（`step_context` 定义）。
"""

import copy
import json
import logging
import time
from pathlib import Path

from backend.data_sources.step_context import (
    DbStepConfigError,
    StepContext,
    StepOutcome,
)
from backend.workspace_files.backups import backup_directory_before_update
from metaweave.core.clean_targets import resolve_ddl_output_dir
from metaweave.core.metadata.generator import (
    MetadataGenerator,
    validate_exclude_tables,
)
from metaweave.core.metadata.generation_config import MetadataGenerationConfig

logger = logging.getLogger("backend.data_sources")

DDL_STEP_NAME = "ddl"
_EMPTY_SCOPE_MESSAGE = "选中范围内没有可采集对象"
# 采集成功时写入受管 DDL 目录的文件清单（doc 16 §4/§5）：发布读取前
# 据此核对缺件。与发布侧 backend.profile_loading.ddl_loader 的同名常量
# 必须一致（测试锁定）。
DDL_MANIFEST_NAME = "manifest.json"


def build_managed_output_snapshot(
    main_config: dict, source_id: str, workspace_id: str, db_name: str, selected_schemas
) -> dict:
    """无凭据的公共工作区快照（doc 19.1 §3）：DDL/JSON 与 MD 共用。

    深拷贝主配置后以登记库身份（`db_name`/`selected_schemas`）覆盖
    `database.database`/`database.schemas`，输出键全部指向同一工作区
    （各经受管 resolver 校验；`cql_directory` 为 rel 发布生成 cql 使用）；
    连接字段显式置 ``None``——MD 等文件步骤不解密密码也不连源库，误用
    时由 `DatabaseConnector` 在建连前拒绝。
    """
    ddl_dir = resolve_ddl_output_dir(source_id, workspace_id)
    workspace_root = ddl_dir.parent
    snapshot = copy.deepcopy(main_config)
    # 非对象节点在归一化前拒绝（doc 19.1 §8 验收 2）
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
            # rel 的 writer 读 output.rel_directory（doc 23.1 §5.2）
            "rel_directory": str(workspace_root / "rel"),
            # dim 步骤产物目录（doc 6 §2：dim_config/ 更名 dim/）
            "dim_tables_directory": str(workspace_root / "dim"),
            # rel 发布的 CQLGenerator 读 output.cql_directory（doc 23.2 §5.1）
            "cql_directory": str(workspace_root / "cql"),
        }
    )
    snapshot["output"] = output
    return snapshot


def build_managed_ddl_config(
    main_config: dict, source, password, workspace_id: str
) -> dict:
    """受管 DDL 的内存配置快照（doc 16 §4）：深拷贝后覆盖，不落盘。

    在公共工作区快照之上追加登记源连接参数（host、port、库名、用户、
    解密密码与 `sslmode`，显式覆盖含 None，doc 18.1 §3.2）。
    """
    snapshot = build_managed_output_snapshot(
        main_config,
        source.source_id,
        workspace_id,
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

    `result.success` 为真、无失败对象、处理对象数大于零、去重后的
    `.sql` 输出数与成功对象数相等，全部输出位于本次受管 DDL 目录内且
    真实落盘，目录实际 `.sql` 文件与输出清单完全一致；任一不满足判失败。
    零对象有意失败，避免一次空集发布删除已有当前 DDL。
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
    declared = {path.resolve() for path in sql_outputs}
    if len(declared) != result.processed_tables:
        return False, "DDL 输出文件数与成功对象数不一致"
    for path in (Path(item) for item in result.output_files):
        if path.resolve().parent != ddl_dir:
            return False, "存在不在本次受管 DDL 目录内的输出文件"
        if not path.is_file() or path.is_symlink():
            return False, "存在声称成功但未实际落盘的输出文件"
    actual = {path.resolve() for path in ddl_dir.glob("*.sql")}
    if actual != declared:
        return False, "受管 DDL 目录的实际输出文件与清单不一致"
    return True, ""


def _write_ddl_manifest(ddl_dir: Path, result) -> None:
    """采集成功后把本次完整文件清单写进受管 DDL 目录（doc 16 §4）。"""
    names = sorted(
        {Path(path).name for path in result.output_files if str(path).endswith(".sql")}
    )
    (ddl_dir / DDL_MANIFEST_NAME).write_text(
        json.dumps({"files": names}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _validate_max_workers(max_workers) -> None:
    if isinstance(max_workers, bool) or not isinstance(max_workers, (int, type(None))):
        raise DbStepConfigError(f"max_workers 必须为正整数（检测到: {max_workers!r}）")
    if max_workers is not None and max_workers < 1:
        raise DbStepConfigError(f"max_workers 必须为正整数（检测到: {max_workers!r}）")


def run_ddl_step(ctx: StepContext, *, max_workers=None) -> StepOutcome:
    """执行受管 DDL 步骤：快照 → 整目录备份 → 生成 → 核对 → 写清单。

    备份失败告警后继续生成（doc 6 §6 生成场景策略）；run_step 状态与
    采集 marker 由编排层写。
    """
    started = time.perf_counter()
    _validate_max_workers(max_workers)
    password = ctx.decrypt_password()
    snapshot = build_managed_ddl_config(
        ctx.main_config, ctx.source, password, ctx.workspace_id
    )
    ddl_dir = resolve_ddl_output_dir(ctx.source_id, ctx.workspace_id)
    if Path(snapshot["output"]["ddl_directory"]) != ddl_dir:
        raise DbStepConfigError("受管 DDL 输出目录与工作区不一致")
    # 非法生成配置在备份之前失败（前置条件 → blocked，不写 marker）
    try:
        MetadataGenerationConfig.from_config(snapshot)
        validate_exclude_tables(snapshot.get("database") or {})
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None

    backup_directory_before_update(ddl_dir)
    try:
        result = MetadataGenerator(ctx.config_path, config=snapshot).generate(
            step=DDL_STEP_NAME,
            max_workers=max_workers if max_workers is not None else 4,
        )
    except Exception as exc:
        logger.error(
            "DDL 采集执行失败 type=%s source_id=%s workspace_id=%s",
            type(exc).__name__,
            ctx.source_id,
            ctx.workspace_id,
        )
        return StepOutcome(
            success=False, message=f"DDL 采集失败（{type(exc).__name__}）"
        )

    ok, reason = verify_ddl_collection(result, ddl_dir)
    if ok:
        try:
            _write_ddl_manifest(ddl_dir, result)
        except OSError:
            ok = False
            reason = "DDL 采集清单写入失败"
    if not ok:
        return StepOutcome(success=False, message=reason)
    output_files = [
        f"ddl/{path.name}"
        for path in sorted(ddl_dir.glob("*.sql"), key=lambda item: item.name)
    ]
    output_files.append(f"ddl/{DDL_MANIFEST_NAME}")
    return StepOutcome(
        success=True,
        message=f"受管 DDL 采集完成（{result.processed_tables} 个对象，"
        f"{time.perf_counter() - started:.1f}s）",
        output_files=output_files,
    )
