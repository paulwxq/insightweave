"""采集步骤执行的共享上下文与结果（doc 6.2 §7.1）。

运行编排层在创建运行后装配 `StepContext`（数据源快照、主配置、工作区与
运行身份、解析后的步骤参数），各步骤 worker 只消费该上下文并返回
`StepOutcome`；作业状态与 run_step 行由编排层写回，worker 不再直接操作
平台库登记状态。
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from backend.data_sources.repository import get_db_source
from backend.persistence.models import DbSourceInfo
from backend.persistence.platform_db import PlatformSettings
from metaweave.core.clean_targets import resolve_workspace_root
from metaweave.core.metadata.execution import load_main_config_snapshot
from metaweave.utils.source_id import normalize_source_id, normalize_workspace_id

logger = logging.getLogger("backend.data_sources")

METADATA_CONFIG = "configs/metadata_config.yaml"


class DbStepConfigError(ValueError):
    """参数、主配置或数据源登记错误；沿用既有 400/exit 2 映射。"""


class SourceDisabledError(DbStepConfigError):
    """数据源已禁用，不能执行步骤（沿用现有 409 映射）。"""


class StepPreconditionError(Exception):
    """步骤前置条件不满足：编排层记 blocked、不写采集 marker（doc 6.2 §6.4）。"""


@dataclass
class StepContext:
    """一个采集步骤的执行上下文（工作区定位 + 运行身份）。"""

    settings: PlatformSettings
    source: DbSourceInfo  # 运行开始时的登记快照（detached）
    main_config: dict
    config_path: Path
    source_id: str  # db_src_1 等短 ID
    workspace_id: str  # ws_1 等短 ID
    run_id: UUID  # 本次 collection_run_id
    step_name: str
    step_params: dict | None  # 本次实际生效参数

    @property
    def workspace_root(self) -> Path:
        return resolve_workspace_root(self.source_id, self.workspace_id)

    def decrypt_password(self) -> str:
        from backend.security.source_password import decrypt_password

        return decrypt_password(self.source.password_ciphertext)


@dataclass
class StepOutcome:
    """步骤 worker 的执行结果；状态落库与 marker 由编排层完成。"""

    success: bool
    message: str
    # 实际读取的工作区文件：(工作区相对路径, 所在目录 marker 的 collection_run_id|None)
    input_files: list[tuple[str, str | None]] = field(default_factory=list)
    # 成功生成并通过检查的文件（工作区相对路径）
    output_files: list[str] = field(default_factory=list)
    # 受控准备失败（前置条件不满足）时置 True：编排层记 blocked、不写 marker
    blocked: bool = False


def load_step_context(
    settings: PlatformSettings,
    *,
    source_id: str,
    workspace_id: str,
    run_id: UUID,
    step_name: str,
    step_params: dict | None,
    config_path: str | Path = METADATA_CONFIG,
) -> StepContext:
    """装配步骤上下文：校验短 ID、读取登记源与主配置。

    数据源不存在/禁用/配置非法按既有受控错误抛出（请求级 404/409/400），
    由运行创建入口使用；后台执行期调用方将这些异常记为步骤失败。
    """
    source_id = normalize_source_id(source_id)
    workspace_id = normalize_workspace_id(workspace_id)
    from backend.persistence.platform_db import platform_session

    with platform_session(settings) as session:
        source = get_db_source(session, source_id)
        if source is None:
            raise DbStepConfigError("未找到指定 source_id 的数据源")
        if not source.is_active:
            raise SourceDisabledError("该数据源已禁用，不能执行作业")
        # session 关闭前取回快照字段（ORM 对象随会话失效，复制必要字段）
        snapshot = DbSourceInfo(
            source_id=source.source_id,
            display_name=source.display_name,
            description=source.description,
            db_type=source.db_type,
            host=source.host,
            port=source.port,
            db_name=source.db_name,
            username=source.username,
            password_ciphertext=source.password_ciphertext,
            sslmode=source.sslmode,
            db_version=source.db_version,
            selected_schemas=list(source.selected_schemas),
            is_active=source.is_active,
        )
    main_config = load_main_config_snapshot(resolve_config_path(config_path))
    return StepContext(
        settings=settings,
        source=snapshot,
        main_config=main_config,
        config_path=Path(config_path),
        source_id=source_id,
        workspace_id=workspace_id,
        run_id=run_id,
        step_name=step_name,
        step_params=step_params,
    )


def resolve_config_path(config_path: str | Path) -> Path:
    from metaweave.utils.file_utils import resolve_project_path

    return resolve_project_path(config_path)
