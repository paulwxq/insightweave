"""rel 发布的输入检查与锚点快照（doc 23.2、6 §4.3、6.2 §8.2）。

- ``check_rel_output_ready``：宽松产物就绪（纯文件面）；
- ``inspect_rel_inputs``：json/domain/rel 三目录的 marker 与内容检查
  （集中预检与执行期复查共用）；
- ``capture_rel_precheck_snapshot``：登记快照 + 三个目录 marker 运行 +
  rel json 指纹 + 主配置快照。

跨步骤采集完成时间的新鲜度检查已按 doc 6 §4.3 取消：不再比较 rel 与
json/domain 的先后，时间展示交给发布页。内容、端点与配置检查保留。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from backend.data_sources.domain_step_service import verify_domain_artifact
from backend.data_sources.json_step_service import (
    JSON_MANIFEST_NAME,
    ManagedJsonInputError,
    read_managed_json_input,
)
from backend.profile_loading.file_utils import read_regular_text
from backend.profile_loading.json_loader import (
    RegistrationSnapshot,
    read_registration_snapshot,
)
from metaweave.core.clean_targets import (
    resolve_domain_output_dir,
    resolve_json_output_dir,
    resolve_rel_output_dir,
)
from metaweave.core.metadata.execution import load_main_config_snapshot
from metaweave.utils.file_utils import resolve_project_path
from backend.workspace_files.markers import read_collection_marker

logger = logging.getLogger("backend.publishing")

REL_STEP = "rel"
JSON_STEP_FOR_REL = "json"
DOMAIN_STEP_FOR_REL = "domain"
DOMAIN_ARTIFACT_NAME = "db_domains.yaml"
METADATA_CONFIG = "configs/metadata_config.yaml"
TMP_PREFIX = ".tmp"
NOT_READY_THREE_DIRS = "工作区的 JSON/Domain/Rel 产物未就绪，无法发布 Rel"


class RelPrecheckError(ValueError):
    """Rel 发布输入不满足（doc 23.2 §6.2，无新鲜度要求）。"""


@dataclass(frozen=True)
class RelPrecheckSnapshot:
    """集中预检捕获的锚点包（执行期复查用）。"""

    registration: RegistrationSnapshot
    json_run_id: str
    domain_run_id: str
    rel_run_id: str
    rel_fingerprint: str
    main_config: dict


def check_rel_output_ready(source_id: str, workspace_id: str) -> bool:
    """宽松产物就绪：rel 目录顶层存在非忽略普通文件（点号文件不算）。"""
    try:
        rel_dir = resolve_rel_output_dir(source_id, workspace_id)
        if not rel_dir.is_dir():
            return False
        for path in rel_dir.iterdir():
            if path.name.startswith("."):
                continue
            if path.is_file() and not path.is_symlink():
                return True
        return False
    except (ValueError, OSError) as exc:
        logger.warning(
            "Rel 预检产物目录不可用 source_id=%s workspace_id=%s type=%s",
            source_id,
            workspace_id,
            type(exc).__name__,
        )
        return False


def _require_marker(directory: Path, label: str) -> str:
    marker, error = read_collection_marker(directory)
    if marker is None:
        raise RelPrecheckError(error or f"{label} 目录没有采集 marker")
    if marker.status != "success":
        raise RelPrecheckError(f"{label} 目录采集未成功，不可发布")
    return marker.run_id


def inspect_rel_inputs(
    source_id: str,
    workspace_id: str,
    registration: RegistrationSnapshot,
    *,
    require_marker_dirs: bool = True,
) -> tuple[str, str, str, str]:
    """三目录输入检查：marker + 内容（doc 6 §4.1；无新鲜度比较）。

    返回 (json_run_id, domain_run_id, rel_run_id, rel_json 指纹)。
    """
    json_dir = resolve_json_output_dir(source_id, workspace_id)
    domain_dir = resolve_domain_output_dir(source_id, workspace_id)
    rel_dir = resolve_rel_output_dir(source_id, workspace_id)
    json_run_id = _require_marker(json_dir, "JSON")
    domain_run_id = _require_marker(domain_dir, "Domain")
    rel_run_id = _require_marker(rel_dir, "Rel")

    try:
        read_managed_json_input(source_id, workspace_id, registration.db_name)
    except ManagedJsonInputError as exc:
        raise RelPrecheckError(f"JSON 产物未就绪：{exc}") from None

    domain_yaml_path = domain_dir / DOMAIN_ARTIFACT_NAME
    artifact_ok, artifact_reason = verify_domain_artifact(domain_yaml_path)
    if not artifact_ok:
        raise RelPrecheckError(f"{artifact_reason}；请先执行 Domain 采集")

    json_path = rel_dir / f"{registration.db_name}.relationships_global.json"
    md_path = rel_dir / f"{registration.db_name}.relationships_global.md"
    actual = set()
    for path in rel_dir.iterdir() if rel_dir.is_dir() else []:
        if (
            path.name.startswith(TMP_PREFIX)
            or path.name.startswith(".")
            or path.is_dir()
        ):
            continue
        if path.is_symlink() or not path.is_file():
            raise RelPrecheckError(f"受管 Rel 目录存在非常规文件: {path.name}")
        actual.add(path.name)
    if actual != {json_path.name, md_path.name}:
        raise RelPrecheckError(
            "Rel 产物目录应为恰好 json 与 md 两份文件，请重跑 Rel 后再发布"
        )
    status, content = read_regular_text(json_path, "rel json")
    if status != "ok":
        raise RelPrecheckError("Rel 产物不可读，请重跑 Rel 后再发布")
    try:
        data = json.loads(content)
    except ValueError:
        raise RelPrecheckError("Rel 产物 JSON 无法解析，请重跑 Rel 后再发布") from None
    if (
        not isinstance(data, dict)
        or data.get("database") != registration.db_name
        or not isinstance(data.get("relationships"), list)
        or not isinstance(data.get("statistics"), dict)
    ):
        raise RelPrecheckError(
            "Rel 产物结构不合法（库名/relationships/statistics），请重跑 Rel"
        )
    fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return json_run_id, domain_run_id, rel_run_id, fingerprint


def build_rel_input_manifest(
    source_id: str,
    workspace_id: str,
    json_run_id: str,
    domain_run_id: str,
    rel_run_id: str,
    database_name: str,
) -> list[tuple[str, str | None]]:
    """Rel 发布 input_manifest 条目：json/domain/rel 全部实际读取文件。"""
    json_dir = resolve_json_output_dir(source_id, workspace_id)
    entries = [
        (f"json/{path.name}", json_run_id)
        for path in sorted(json_dir.glob("*.json"), key=lambda item: item.name)
        if path.name != JSON_MANIFEST_NAME
    ]
    entries.append((f"domain/{DOMAIN_ARTIFACT_NAME}", domain_run_id))
    entries.append((f"rel/{database_name}.relationships_global.json", rel_run_id))
    entries.append((f"rel/{database_name}.relationships_global.md", rel_run_id))
    return entries


def capture_rel_precheck_snapshot(
    session, source_id: str, workspace_id: str
) -> RelPrecheckSnapshot:
    """集中预检锚点打包（登记快照 + marker 运行 + 指纹 + 主配置）。"""
    registration = read_registration_snapshot(session, source_id)
    json_run_id, domain_run_id, rel_run_id, fingerprint = inspect_rel_inputs(
        source_id, workspace_id, registration
    )
    # 主配置快照合法（generator 构造期校验 composite_score_threshold）
    from metaweave.core.cql_generator.generator import CQLGenerator

    config_path = resolve_project_path(METADATA_CONFIG)
    main_config = load_main_config_snapshot(config_path)
    try:
        CQLGenerator(config_path, config=main_config)
    except ValueError as exc:
        raise RelPrecheckError(str(exc)) from None
    return RelPrecheckSnapshot(
        registration=registration,
        json_run_id=json_run_id,
        domain_run_id=domain_run_id,
        rel_run_id=rel_run_id,
        rel_fingerprint=fingerprint,
        main_config=main_config,
    )
