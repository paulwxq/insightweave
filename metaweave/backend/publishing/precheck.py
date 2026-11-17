"""发布集中预检（doc 6.2 §6.3.2、§8.2；doc 7.2 §5.2.1 准入最小集）。

同一套检查供三处使用：
- `GET /api/workspaces/{workspace_id}/publish-check`：固定返回全部 8 个
  发布步骤的条件、原因与输入目录采集时间（doc 6.2 §8.2.2）；dim_value/
  domain 只做准入最小集的文件侧核对（marker + 候选来源可读），不冒充
  对有效配置的校验；
- `POST /api/publish-jobs/{job_id}/precheck`：按"引用版本默认值 + 本次
  允许的覆盖"解析有效参数后逐部骤校验（doc 7.2 §5.2），不创建运行；
- `create_publish_run` 与执行期复查：同一份有效配置整单校验，任一步骤
  不合格整单 400 / 记 blocked（§6.4；doc 7.2 §5.1/§5.3）。

跨步骤采集完成时间的新鲜度检查已取消（doc 6 §4.3）；采集时间只用于
页面展示，不参与判定。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.profile_loading.ddl_loader import check_ddl_output_ready
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


def _marker_run_id(source_id: str, workspace_id: str, name: str) -> "str | None":
    """读取某采集目录 marker 的 run_id（输入清单归属用；无 marker 归 None）。"""
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
    marker, _error = read_collection_marker(resolver_by_dir[name](source_id, workspace_id))
    return marker.run_id if marker else None


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
    # 既有业务错误码（如 dim_value_empty_tables），供上层透传到 details
    code: str | None = None
    # 实际读取的文件（workspace 相对路径 + 所在目录 marker 的采集运行）
    input_files: tuple[tuple[str, "str | None"], ...] = ()
    # 步骤附加载荷（如 dim_value 的归一配置、domain 的有效清单、rel 的锚点快照）
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
    """工作区只读检查（publish-check GET，doc 7.2 §5.2.1 准入最小集）。

    domain marker 已由调用方核验；这里只复核同工作区 MD 清单合法可读
    （domain 发布的实际依赖），不再要求 db_domains.yaml 存在或可解析。
    """
    from backend.profile_loading.domain_loader import (
        DomainPublishInputError,
        _read_md_manifest,
    )

    try:
        _read_md_manifest(source_id, workspace_id)
    except DomainPublishInputError as exc:
        return False, f"MD 采集清单不可用（{exc}）", []
    return True, None, [
        ("md/manifest.json", _marker_run_id(source_id, workspace_id, "md"))
    ]


def _domain_effective_check(
    session,
    source_id: str,
    workspace_id: str,
    effective_params: "dict | None",
):
    """有效清单校验（doc 7.2 §7.4）：版本默认/运行覆盖，与 YAML 解耦。

    校验规则与文件路径完全相同（domain_rows_from_config 复用同一映射）；
    成功时 payload 为规范化 domains 清单，供执行期在锁下重算发布行。
    """
    from backend.jobs.job_params import StepConfigError, _validate_domains_param
    from backend.profile_loading.domain_loader import (
        DomainPublishInputError,
        domain_rows_from_config,
    )
    from backend.profile_loading.json_loader import read_registration_snapshot

    params = effective_params.get("domain")
    if not isinstance(params, dict) or "domains" not in params:
        return (
            False,
            "domain 缺少必需配置 step_config.domain.domains："
            "请编辑作业并保存新版本后重新运行",
            None,
            [],
            None,
        )
    try:
        domains = _validate_domains_param(params.get("domains"))
    except StepConfigError as exc:
        return False, str(exc), None, [], None
    if not domains:
        return (
            False,
            "domain 未配置任何业务域：请为 domain 添加业务域并指定表归属，"
            "或取消勾选 domain",
            None,
            [],
            None,
        )
    registration = read_registration_snapshot(session, source_id)
    try:
        rows = domain_rows_from_config(domains, source_id, workspace_id, registration)
    except DomainPublishInputError as exc:
        return False, str(exc), None, [], None
    if not rows:
        return (
            False,
            "Domain 配置没有任何有效表归属，请为业务域指定表或取消勾选 domain",
            None,
            [],
            None,
        )
    md_run_id = _marker_run_id(source_id, workspace_id, "md")
    files = [("md/manifest.json", md_run_id)]
    files.extend(
        (f"md/{row['db_name']}.{row['schema_name']}.{row['table_name']}.md", md_run_id)
        for row in rows
    )
    return True, None, None, files, {"domains": domains}


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


def _dim_value_check(
    session,
    source_id: str,
    workspace_id: str,
    markers,
    db_name: str | None,
    effective_params: "dict | None",
):
    """dim_value 预检（doc 7.2 §5.2.1 准入最小集）。

    - effective_params 非 None（创建运行/执行期复查）：校验本次有效清单
      （版本默认 + 允许覆盖），构造可注入的 DimTablesConfig 载荷；不再读取
      dim_tables.yaml；画像库名/schema 须落在登记边界内；
    - effective_params 为 None（publish-check GET 工作区只读检查）：只核对
      JSON 画像候选来源可读，不冒充对有效配置的校验。
    dim 目录成功 marker 均由 require_success_markers 先行核验。
    """
    if effective_params is None:
        from backend.profile_loading.dim_value_config_api import _iter_json_products

        json_dir = resolve_json_output_dir(source_id, workspace_id)
        if not json_dir.is_dir() or not any(_iter_json_products(json_dir)):
            return False, "当前工作区没有可配置的维表候选（JSON 画像缺失）", None, [], None
        return True, None, None, [], None
    if db_name is None:
        return False, "登记不可用", [], None
    from backend.profile_loading.dim_value_config_api import (
        build_dim_tables_from_params,
    )
    from backend.profile_loading.json_loader import read_registration_snapshot

    params = effective_params.get("dim_value")
    if not isinstance(params, dict) or "tables" not in params:
        return (
            False,
            "dim_value 缺少必需配置 step_config.dim_value.tables："
            "请编辑作业并保存新版本后重新运行",
            None,
            [],
            None,
        )
    registration = read_registration_snapshot(session, source_id)
    cfg, reason, code, files = build_dim_tables_from_params(
        params.get("tables"), source_id, workspace_id, registration
    )
    if cfg is None:
        return False, reason, code, [], None
    return True, None, None, files, cfg


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
    effective_params: "dict | None" = None,
) -> StepPrecheckResult:
    """检查单个发布步骤的全部输入（marker + 文件 + 内容/配置）。

    effective_params 非 None 时（创建运行、执行期复查、作业级预检），
    dim_value/domain 按本次有效清单校验并产出可注入载荷；为 None 时
    （publish-check GET 工作区只读检查）只做准入最小集的文件侧核对。
    """
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
        if effective_params is None:
            ready, reason, files = _domain_files(source_id, workspace_id, markers)
            return StepPrecheckResult(step, ready, reason, None, tuple(files))
        ready, reason, code, files, payload = _domain_effective_check(
            session, source_id, workspace_id, effective_params
        )
        return StepPrecheckResult(step, ready, reason, code, tuple(files), payload)
    elif step == "table_schema":
        ready, reason, files = _table_schema_files(
            source_id, workspace_id, markers, db_name
        )
    elif step == "dim_value":
        ready, reason, code, files, payload = _dim_value_check(
            session, source_id, workspace_id, markers, db_name, effective_params
        )
        return StepPrecheckResult(step, ready, reason, code, tuple(files), payload)
    elif step == "rel":
        ready, reason, files, payload = _rel_check(session, source_id, workspace_id)
        return StepPrecheckResult(step, ready, reason, None, tuple(files), payload)
    elif step == "sql":
        ready, reason, files = _sql_files(source_id, workspace_id, markers)
    else:  # pragma: no cover - 固定白名单
        return StepPrecheckResult(step, False, f"未知发布步骤: {step}")
    return StepPrecheckResult(step, ready, reason, None, tuple(files))


def precheck_steps(
    session,
    source_id: str,
    workspace_id: str,
    steps: "list[str] | tuple[str, ...]",
    *,
    db_name: str | None,
    effective_params: "dict | None" = None,
) -> dict[str, StepPrecheckResult]:
    """对所选步骤集中预检；任一失败由调用方整单 400（doc 6.2 §6.4）。"""
    results = {}
    for step in steps:
        results[step] = check_step(
            session,
            source_id,
            workspace_id,
            step,
            db_name=db_name,
            effective_params=effective_params,
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
