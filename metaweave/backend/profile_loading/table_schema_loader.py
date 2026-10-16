"""Web table_schema 发布输入与加载器构造，复用核心文件读取逻辑。"""

from copy import deepcopy
from uuid import uuid4

from backend.jobs.repository import get_step_row, STATUS_SUCCESS
from backend.profile_loading.json_loader import inspect_json_profile_context
from metaweave.core.clean_targets import resolve_json_output_dir, resolve_md_output_dir
from metaweave.core.loaders.table_schema_loader import TableSchemaLoader
from metaweave.core.metadata.execution import load_main_config_snapshot
from metaweave.core.table_schema import LoaderOptions
from metaweave.core.table_schema.managed_input import (
    configure_managed_loader,
    prepare_managed_table_schema_input,
)
from metaweave.utils.time_utils import utc_isoformat

TABLE_SCHEMA_STEP = "table_schema"
# doc 20.1 §4.2：Web/API 固定加载字段级对象并跳过空描述字段，不受配置文件影响。
WEB_LOADER_OPTIONS = LoaderOptions(include_columns=True, skip_empty_desc=True)


def inspect_table_schema_context(session, source_id, job_id):
    json_status, _ = inspect_json_profile_context(session, source_id, job_id)
    md = get_step_row(session, job_id, "md")
    if md is None:
        return None, None
    if json_status != STATUS_SUCCESS:
        return json_status, md.updated_at
    return md.last_status, md.updated_at


def check_table_schema_output_ready(source_id, job_id, json_marker, database_name):
    if json_marker is None or database_name is None:
        return False
    try:
        # 与执行阶段同一函数、同一固定选项；只读文件，不连 Embedding / Milvus
        prepare_managed_table_schema_input(
            resolve_json_output_dir(source_id, job_id),
            resolve_md_output_dir(source_id, job_id),
            source_id,
            WEB_LOADER_OPTIONS,
            utc_isoformat(json_marker),
            database_name,
        )
    except (ValueError, OSError, KeyError, TypeError):
        return False
    return True


def build_table_schema_loader(source_id, job_id, publish_id=None):
    snapshot = load_main_config_snapshot("configs/metadata_config.yaml")
    # 请求各自持有连接；关闭本次连接不影响其他源同时发布。
    snapshot["vector_database"]["providers"]["milvus"][
        "alias"
    ] = f"table_schema_{uuid4().hex}"
    loaders = deepcopy(snapshot.get("loaders", {}))
    configure_managed_loader(loaders, source_id, job_id)
    # 强制覆盖运行值（不是默认值）：仅在加载器配置的深拷贝中改写，
    # 不修改主配置快照，也不影响调试 CLI 的配置驱动行为。
    cfg = loaders["table_schema_loader"]
    options = dict(cfg.get("options") or {})
    options["include_columns"] = WEB_LOADER_OPTIONS.include_columns
    options["skip_empty_desc"] = WEB_LOADER_OPTIONS.skip_empty_desc
    cfg["options"] = options
    return TableSchemaLoader(loaders, metadata_config=snapshot, publish_id=publish_id)
