"""发布集中预检（doc 6.2 §6.3.2、§8.2）：marker + 必要文件 + 内容与配置。

同一套检查供两处使用：
- `GET /api/workspaces/{workspace_id}/publish-check`：固定返回全部 8 个
  发布步骤的条件、原因与输入目录采集时间（doc 6.2 §8.2.2）；
- `create_publish_run`：创建运行前对本次所选步骤集中预检，任一步骤不
  合格整单 400，不生成运行、不写业务记录（§6.4）。

跨步骤采集完成时间的新鲜度检查已取消（doc 6 §4.3）；采集时间只用于
页面展示，不参与判定。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.profile_loading.ddl_loader import check_ddl_output_ready
from backend.profile_loading.dim_value_config_api import classify_dim_tables_yaml
from backend.profile_loading.dim_value_loader import (
    DIM_CONFIG_ARTIFACT_NAME,
    check_dim_value_output_ready,
)
from backend.profile_loading.domain_loader import check_domain_output_ready
from backend.profile_loading.json_loader import check_json_output_ready
from backend.profile_loading.rel_loader import (
    RelPrecheckError,
    capture_rel_precheck_snapshot,
)
from backend.profile_loading.sql_loader import check_sql_output_ready
from backend.workspace_files.markers import CollectionMarker, read_collection_marker
from metaweave.core.clean_targets import (
    resolve_db_output_dir,
    resolve_ddl_output_dir,
    resolve_dim_output_dir,
    resolve_domain_output_dir,
    resolve_json_output_dir,
    resolve_md_output_dir,
    resolve_rel_output_dir,
    resolve_sql_output_dir,
)

logger = logging.getLogger("backend.publishing")

PUBLISH_STEP_ORDER = (
    "db",
    "ddl",
    "json",
    "domain",
    "table_schema",
    "dim_value",
    "rel",
    "sql",
)

# 每个发布步骤依赖的采集产物目录（doc 6.2 §8.1；cql/ 为发布辅助目录，
# 不要求预先具备采集 marker）
STEP_INPUT_DIRS = {
    "db": ("db",),
    "ddl": ("ddl",),
    "json": ("json",),
    "domain": ("domain",),
    "table_schema": ("json", "md"),
    "dim_value": ("dim",),
    "rel": ("json", "domain", "rel"),
    "sql": ("sql",),
}

_NO_MARKER = "目录没有采集 marker，产物不可用"
_FAILED_MARKER = "目录最近一次采集未成功，产物不可用"


class PublishPrecheckError(ValueError):
    """集中预检不合格：整单 400；携带逐步骤原因（doc 6.2 §6.4）。"""

    def __init__(self, failures: list[dict]):
        message = "；".join(f"{item['step']}: {item['reason']}" for item in failures)
        super().__init__(message or "发布预检未通过")
        self.failures = failures


@dataclass(frozen=True)
class StepPrecheckResult:
    """单个发布步骤的预检结果。"""

    step: str
    ready: bool
    reason: str | None = None
    # 实际读取的文件（workspace 相对路径 + 所在目录 marker 的采集运行）
    input_files: tuple[tuple[str, "str | None"], ...] = ()
    # 步骤附加载荷（如 dim_value 的归一配置、rel 的锚点快照）
    payload: object = None


def _dir_markers(source_id: str, workspace_id: str, dirs: tuple[str, ...]):
    """读取各依赖目录的 marker；返回 [(dir_name, 目录, marker|None, error|None)]。"""
    resolver_by_dir = {
        "db": resolve_db_output_dir,
        "ddl": resolve_ddl_output_dir,
        "json": resolve_json_output_dir,
        "md": resolve_md_output_dir,
        "domain": resolve_domain_output_dir,
        "dim": resolve_dim_output_dir,
        "rel": resolve_rel_output_dir,
        "sql": resolve_sql_output_dir,
    }
    results = []
    for name in dirs:
        directory = resolver_by_dir[name](source_id, workspace_id)
        marker, error = read_collection_marker(directory)
        results.append((name, directory, marker, error))
    return results


def require_success_markers(
    source_id: str, workspace_id: str, dirs: tuple[str, ...]
) -> tuple[dict[str, CollectionMarker], str | None]:
    """目录 marker 全部 success 才通过；返回 {dir: marker} 与失败原因。"""
    markers: dict[str, CollectionMarker] = {}
    for name, _directory, marker, error in _dir_markers(source_id, workspace_id, dirs):
        if marker is None:
            return markers, f"{name}/ {_NO_MARKER}" + (f"（{error}）" if error else "")
        if marker.status != "success":
            return markers, f"{name}/ {_FAILED_MARKER}"
        markers[name] = marker
    return markers, None


def _db_files(
    source_id: str, workspace_id: str, markers
) -> tuple[bool, str | None, list]:
    from backend.profile_loading.db_loader import resolve_db_profile_input_path

    try:
        resolve_db_profile_input_path(source_id, workspace_id)
    except Exception:  # noqa: BLE001
        return False, "db/database.json 缺失或不可读", []
    run_id = markers["db"].run_id
    return True, None, [("db/database.json", run_id)]


def _ddl_files(source_id: str, workspace_id: str, markers):
    if not check_ddl_output_ready(source_id, workspace_id):
        return False, "ddl/ 产物缺失或与采集清单不一致", []
    run_id = markers["ddl"].run_id
    directory = resolve_ddl_output_dir(source_id, workspace_id)
    files = [
        (f"ddl/{path.name}", run_id)
        for path in sorted(directory.glob("*.sql"), key=lambda p: p.name)
    ]
    return True, None, files


def _json_files(source_id: str, workspace_id: str, markers, db_name: str | None):
    if db_name is None:
        return False, "登记不可用", []
    if not check_json_output_ready(source_id, workspace_id, db_name):
        return False, "json/ 产物缺失、清单不符或未通过校验", []
    run_id = markers["json"].run_id
    directory = resolve_json_output_dir(source_id, workspace_id)

    files = [
        (f"json/{path.name}", run_id)
        for path in sorted(directory.glob("*.json"), key=lambda p: p.name)
    ]
    return True, None, files


def _domain_files(source_id: str, workspace_id: str, markers):
    if not check_domain_output_ready(source_id, workspace_id):
        return False, "domain/db_domains.yaml 缺失或不可读", []
    return True, None, [("domain/db_domains.yaml", markers["domain"].run_id)]


def _table_schema_files(
    source_id: str, workspace_id: str, markers, db_name: str | None
):
    from backend.profile_loading.table_schema_loader import (
        check_table_schema_output_ready,
    )

    json_run = markers["json"].run_id
    if db_name is None:
        return False, "登记不可用", []
    if not check_table_schema_output_ready(source_id, workspace_id, json_run, db_name):
        return False, "JSON、MD 产物缺失或不匹配，请重新采集", []
    json_dir = resolve_json_output_dir(source_id, workspace_id)
    md_dir = resolve_md_output_dir(source_id, workspace_id)
    from backend.data_sources.json_step_service import JSON_MANIFEST_NAME

    files = [
        (f"json/{path.name}", json_run)
        for path in sorted(json_dir.glob("*.json"), key=lambda p: p.name)
        if path.name != JSON_MANIFEST_NAME
    ]
    md_run = markers["md"].run_id
    files.extend(
        (f"md/{path.name}", md_run)
        for path in sorted(md_dir.glob("*.md"), key=lambda p: p.name)
    )
    return True, None, files


def _dim_value_check(source_id: str, workspace_id: str, markers, db_name: str | None):
    """dim_value 三态拦截（doc 22.2 §6.2；A15）：配置无效/零表/未配置。"""
    if not check_dim_value_output_ready(source_id, workspace_id):
        return False, "dim/dim_tables.yaml 缺失或不可读", [], None
    if db_name is None:
        return False, "登记不可用", [], None
    yaml_path = resolve_dim_output_dir(source_id, workspace_id) / (
        DIM_CONFIG_ARTIFACT_NAME
    )
    state, reason, cfg = classify_dim_tables_yaml(yaml_path, db_name)
    if state == "invalid":
        return (
            False,
            f"dim_tables.yaml 配置文件无效（{reason or '配置文件无效'}）",
            [],
            None,
        )
    if state == "empty":
        return False, "dim_tables.yaml 没有任何维度表", [], None
    unconfigured = sorted(
        name for name, table in cfg.tables.items() if table.embedding_col is None
    )
    if unconfigured:
        return (
            False,
            "以下维度表未配置有效 embedding_col: " + "、".join(unconfigured),
            [],
            None,
        )
    return True, None, [("dim/dim_tables.yaml", markers["dim"].run_id)], cfg


def _rel_check(session, source_id: str, workspace_id: str):
    """rel：三目录 marker + 内容检查 + 锚点快照（无新鲜度比较）。"""
    try:
        snapshot = capture_rel_precheck_snapshot(session, source_id, workspace_id)
    except RelPrecheckError as exc:
        return False, str(exc), [], None
    from backend.profile_loading.rel_loader import build_rel_input_manifest

    files = build_rel_input_manifest(
        source_id,
        workspace_id,
        snapshot.json_run_id,
        snapshot.domain_run_id,
        snapshot.rel_run_id,
        snapshot.registration.db_name,
    )
    return True, None, files, snapshot


def _sql_files(source_id: str, workspace_id: str, markers):
    if not check_sql_output_ready(source_id, workspace_id):
        return False, "sql/ 目录没有可发布的 SQL 样例文件", []
    run_id = markers["sql"].run_id
    directory = resolve_sql_output_dir(source_id, workspace_id)
    from metaweave.core.sql_rag.loader import collect_sql_example_input_files

    files = [
        (f"sql/{path.name}", run_id)
        for path in sorted(
            collect_sql_example_input_files(directory), key=lambda p: p.name
        )
    ]
    return True, None, files


def check_step(
    session,
    source_id: str,
    workspace_id: str,
    step: str,
    *,
    db_name: str | None,
) -> StepPrecheckResult:
    """检查单个发布步骤的全部输入（marker + 文件 + 内容/配置）。"""
    dirs = STEP_INPUT_DIRS[step]
    markers, failure = require_success_markers(source_id, workspace_id, dirs)
    if failure is not None:
        return StepPrecheckResult(step, False, failure)

    if step == "db":
        ready, reason, files = _db_files(source_id, workspace_id, markers)
    elif step == "ddl":
        ready, reason, files = _ddl_files(source_id, workspace_id, markers)
    elif step == "json":
        ready, reason, files = _json_files(source_id, workspace_id, markers, db_name)
    elif step == "domain":
        ready, reason, files = _domain_files(source_id, workspace_id, markers)
    elif step == "table_schema":
        ready, reason, files = _table_schema_files(
            source_id, workspace_id, markers, db_name
        )
    elif step == "dim_value":
        ready, reason, files, payload = _dim_value_check(
            source_id, workspace_id, markers, db_name
        )
        return StepPrecheckResult(step, ready, reason, tuple(files), payload)
    elif step == "rel":
        ready, reason, files, payload = _rel_check(session, source_id, workspace_id)
        return StepPrecheckResult(step, ready, reason, tuple(files), payload)
    elif step == "sql":
        ready, reason, files = _sql_files(source_id, workspace_id, markers)
    else:  # pragma: no cover - 固定白名单
        return StepPrecheckResult(step, False, f"未知发布步骤: {step}")
    return StepPrecheckResult(step, ready, reason, tuple(files))


def precheck_steps(
    session,
    source_id: str,
    workspace_id: str,
    steps: "list[str] | tuple[str, ...]",
    *,
    db_name: str | None,
) -> dict[str, StepPrecheckResult]:
    """对所选步骤集中预检；任一失败由调用方整单 400（doc 6.2 §6.4）。"""
    results = {}
    for step in steps:
        results[step] = check_step(
            session, source_id, workspace_id, step, db_name=db_name
        )
    return results


def collect_input_dir_times(source_id: str, workspace_id: str) -> dict[str, dict]:
    """各采集目录的 marker 状态与采集完成时间（发布页展示，doc 6 §4.3）。

    时间取自 marker 文件名时间戳（Asia/Shanghai），不是运行结束时间；
    无 marker 时 status=none。
    """
    info: dict[str, dict] = {}
    for dirs in STEP_INPUT_DIRS.values():
        for name in dirs:
            if name in info:
                continue
            marker_data = _dir_markers(source_id, workspace_id, (name,))
            _dir, directory, marker, error = marker_data[0]
            info[name] = {
                "directory": name,
                "marker_status": marker.status if marker else "none",
                "collection_run_id": marker.run_id if marker else None,
                # ISO 文本（上海时区）；无 marker 时为 None
                "collected_at": (marker.finished_at.isoformat() if marker else None),
                "error": error,
            }
    return info


def normalize_publish_steps(steps: list[str]) -> list[str]:
    """白名单校验、去重并按固定顺序排列（doc 6.2 §8.1，后端落实）。"""
    if not steps:
        raise ValueError("请至少选择一个发布步骤")
    chosen = set()
    for step in steps:
        if step not in PUBLISH_STEP_ORDER:
            raise ValueError(
                "当前仅支持 db、ddl、json、domain、table_schema、dim_value、"
                "rel、sql 步骤"
            )
        chosen.add(step)
    return [step for step in PUBLISH_STEP_ORDER if step in chosen]
