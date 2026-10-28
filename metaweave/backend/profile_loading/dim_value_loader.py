"""Web dim_value 发布的预检上下文、产物就绪检查与受管 loader 构造（doc 22.2 §6.1/§7.1）。"""

from copy import deepcopy
from uuid import uuid4

from metaweave.core.clean_targets import resolve_dim_output_dir
from metaweave.core.dim_value.models import DimTablesConfig
from metaweave.core.loaders.dim_value_loader import DimValueLoader
from metaweave.core.metadata.execution import load_main_config_snapshot
from metaweave.utils.logger import get_metaweave_logger

DIM_VALUE_STEP = "dim_value"
DIM_CONFIG_ARTIFACT_NAME = "dim_tables.yaml"

logger = get_metaweave_logger("dim_value.loader")


def check_dim_value_output_ready(source_id: str, workspace_id: str) -> bool:
    """预检：dim 目录下 dim_tables.yaml 存在且为普通文件。

    不读内容、不数表行数（doc 22.2 §6.1）：零表产物仍可勾选，
    发布时由三态拦截（§6.2）处理。
    """

    try:
        dim_dir = resolve_dim_output_dir(source_id, workspace_id)
        artifact = dim_dir / DIM_CONFIG_ARTIFACT_NAME
        if artifact.is_symlink() or not artifact.is_file():
            logger.warning(
                "dim_value 预检产物不可用 source_id=%s workspace_id=%s",
                source_id,
                workspace_id,
            )
            return False
    except Exception:  # noqa: BLE001
        logger.warning(
            "dim_value 预检产物目录不可解析 source_id=%s workspace_id=%s",
            source_id,
            workspace_id,
        )
        return False
    return True


def build_dim_value_loader(
    source, password, workspace_id, publish_run_id, dim_tables: DimTablesConfig
) -> DimValueLoader:
    """受管 loader 构造（doc 22.2 §7.1，签名写实——锁内源行 + 解密密码 + 注入配置）。"""

    snapshot = load_main_config_snapshot("configs/metadata_config.yaml")
    # 请求各自持有连接；关闭本次连接不影响其他源同时发布。
    snapshot["vector_database"]["providers"]["milvus"][
        "alias"
    ] = f"dim_value_{uuid4().hex}"
    loaders = deepcopy(snapshot.get("loaders", {}))

    dim_cfg = loaders.get("dim_loader") or {}
    dim_cfg["config_file"] = str(
        resolve_dim_output_dir(source.source_id, workspace_id)
        / DIM_CONFIG_ARTIFACT_NAME
    )
    loaders["dim_loader"] = dim_cfg

    # 登记源连接覆盖（共六键，审核修订 5：database 键是整条链的锚，勿只抄外层五键）
    database_node = dict(snapshot.get("database") or {})
    database_node.update(
        {
            "database": source.db_name,
            "host": source.host,
            "port": source.port,
            "user": source.username,
            "password": password,
            "sslmode": source.sslmode,
        }
    )
    snapshot["database"] = database_node

    return DimValueLoader(
        loaders,
        metadata_config=snapshot,
        source_id=source.source_id,
        publish_id=publish_run_id,
        dim_tables=dim_tables,
    )
