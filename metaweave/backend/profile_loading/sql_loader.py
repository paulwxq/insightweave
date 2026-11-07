"""Web sql 发布：预检 inspector、产物就绪检查与受管加载器构造（doc 25.2）。

sql-rag 无编辑 API：唯一受管写入方是采集步骤（必刷新步骤行 updated_at），
发布侧不设三态拦截与内容注入，run_marker 锁下复查足够（§6.1）。
"""

from __future__ import annotations

import logging
from copy import deepcopy
from uuid import uuid4

from metaweave.core.clean_targets import resolve_sql_output_dir
from metaweave.core.metadata.execution import load_main_config_snapshot
from metaweave.core.sql_rag.loader import (
    SQLExampleLoader,
    collect_sql_example_input_files,
)

logger = logging.getLogger("backend.publishing")

SQL_STEP = "sql"


def check_sql_output_ready(source_id, workspace_id) -> bool:
    """宽松产物就绪（需求 4 的落点）：sql 目录顶层 ≥1 个非忽略 `*.json` 普通文件。

    枚举经 metaweave 侧共享 helper（`collect_sql_example_input_files`），
    不另写一份（§5/§7.2 第 2 条）。目录不可用兜底降级未就绪 + warning，
    不让 check_publication 500（check_rel_output_ready 同构）。
    """
    try:
        sql_dir = resolve_sql_output_dir(source_id, workspace_id)
        return bool(collect_sql_example_input_files(sql_dir))
    except (ValueError, OSError) as exc:
        logger.warning(
            "sql 预检产物目录不可用 source_id=%s workspace_id=%s type=%s",
            source_id,
            workspace_id,
            type(exc).__name__,
        )
        return False


def build_sql_example_loader(source_id, workspace_id, publish_run_id):
    """受管加载器构造（§6.2）：主配置快照 + Milvus alias 唯一化 + 受管 input_dir。

    不连源库、不依赖登记 db_name（输入按 `*.json` 通配），无需登记连接注入。
    """
    snapshot = load_main_config_snapshot("configs/metadata_config.yaml")
    # 请求各自持有连接；关闭本次连接不影响其他源同时发布。
    snapshot["vector_database"]["providers"]["milvus"]["alias"] = f"sql_{uuid4().hex}"
    loaders = deepcopy(snapshot.get("loaders", {}))
    cfg = loaders.setdefault("sql_loader", {})
    cfg["input_dir"] = str(resolve_sql_output_dir(source_id, workspace_id))
    return SQLExampleLoader(
        loaders,
        metadata_config=snapshot,
        source_id=source_id,
        publish_run_id=publish_run_id,
    )
