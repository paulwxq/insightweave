"""doc 23.2 §6.1/§6.2：rel 发布的预检数据、严格断言与锚点快照。

- ``check_rel_output_ready``：宽松产物就绪（D6 解冻口径，纯文件面）；
- ``inspect_rel_context``：check_publication / publish_steps 共用的 rel 行检查；
- ``assert_rel_publish_strict``：执行预检第 1–5 条（§6.2 控制流块，仅当
  chosen 含 rel 时在 publish_id 之前调用）；
- ``capture_rel_precheck_snapshot``：第 2+6 条锚点打包（登记快照 + json/
  domain 两行 marker + rel json 指纹 + 主配置快照）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from backend.data_sources.domain_step_service import verify_domain_artifact
from backend.data_sources.json_step_service import (
    ManagedJsonInputError,
    read_managed_json_input,
)
from backend.jobs.repository import assert_workspace_owner, get_step_row
from backend.persistence.platform_db import platform_session
from backend.profile_loading.json_loader import read_registration_snapshot
from backend.profile_loading.file_utils import read_regular_text
from backend.publishing.errors import PublicationStepNotReadyError
from metaweave.core.clean_targets import (
    resolve_domain_output_dir,
    resolve_rel_output_dir,
)
from metaweave.core.metadata.execution import load_main_config_snapshot
from metaweave.utils.file_utils import resolve_project_path

logger = logging.getLogger("backend.publishing")

REL_STEP = "rel"
JSON_STEP_FOR_REL = "json"
DOMAIN_STEP_FOR_REL = "domain"
DOMAIN_ARTIFACT_NAME = "db_domains.yaml"
METADATA_CONFIG = "configs/metadata_config.yaml"
BACKUP_DIR_NAME = ".backup"
TMP_PREFIX = ".tmp"
NOT_READY_THREE_ROWS = "同一作业的 JSON/Domain/Rel 步骤未成功，无法发布 Rel"


@dataclass(frozen=True)
class RelPrecheckSnapshot:
    """§6.2 第 6 条锚点包（经 registrations 旁路携带到 ``_publish_rel``）。"""

    registration: object  # RegistrationSnapshot（updated_at/db_name/selected_schemas）
    json_marker: datetime
    domain_marker: datetime
    rel_fingerprint: str
    main_config: dict


def check_rel_output_ready(source_id, job_id) -> bool:
    """宽松产物就绪（D6 解冻口径）：rel 目录顶层存在非忽略普通文件。

    忽略规则与执行预检共用（§6.2）：``.tmp*`` 前缀与 ``.backup/`` 子目录不算
    数——PUT 崩溃残留的临时文件不会单独造成假就绪。纯文件面检查，不访问
    平台库（与 ``check_dim_value_output_ready`` 同构）。目录不可用（路径含
    符号链接的 ValueError、不可读的 OSError）兜底降级为未就绪 + warning——
    不让整个 check_publication 500（check_domain/dim_value 同款）。
    """
    try:
        rel_dir = resolve_rel_output_dir(source_id, job_id)
        if not rel_dir.is_dir():
            return False
        for path in rel_dir.iterdir():
            if path.name.startswith(TMP_PREFIX):
                continue
            if path.is_file() and not path.is_symlink():
                return True
        return False
    except (ValueError, OSError) as exc:
        logger.warning(
            "Rel 预检产物目录不可用 source_id=%s job_id=%s type=%s",
            source_id,
            job_id,
            type(exc).__name__,
        )
        return False


def inspect_rel_context(session, source_id: UUID, job_id: UUID):
    """rel 行检查（不读文件）：返回 (last_status, updated_at)。"""

    from backend.data_sources.repository import get_db_source
    from backend.profile_loading.db_loader import (
        MetaLoadSourceDisabledError,
        MetaLoadSourceNotFoundError,
    )

    source = get_db_source(session, source_id)
    if source is None:
        raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
    if not source.is_active:
        raise MetaLoadSourceDisabledError("指定数据源已禁用，不能加载画像")
    assert_workspace_owner(session, job_id, source_id)
    step = get_step_row(session, job_id, REL_STEP)
    if step is None:
        return None, None
    return step.last_status, step.updated_at


def _require_step_marker(session, job_uuid: UUID, step_name: str) -> datetime:
    """三行存在性 + success 就地复查（§6.2 块内职责终稿：不依赖外层闸）。"""
    row = get_step_row(session, job_uuid, step_name)
    if row is None or row.last_status != "success":
        raise PublicationStepNotReadyError(NOT_READY_THREE_ROWS)
    return row.updated_at


def _assert_fresh_ordering(
    rel_marker: datetime, json_marker: datetime, domain_marker: datetime
) -> None:
    """新鲜度排序（§6.2：rel 严格晚于 json/domain，等值按过期）。

    assert 与 capture 是两个无锁会话——上游重跑可能恰在其间提交，使
    assert 用旧时间戳判过、capture 却锚定新 marker（锁下核对该新值「自
    预检未变」必然通过，旧 rel 便配新 json 发布）。故排序必须以**将被
    锚定的那组 marker** 复查：capture 期再跑一次本函数即封住该窗口。
    """
    stale = [
        label
        for label, marker in (("JSON", json_marker), ("Domain", domain_marker))
        if not (rel_marker > marker)
    ]
    if stale:
        raise PublicationStepNotReadyError(
            f"同一作业的 {' 与 '.join(stale)} 已重跑，Rel 产物已过期；"
            "请重跑 Rel 后再发布"
        )


def assert_rel_publish_strict(settings, source_uuid: UUID, job_uuid: UUID) -> None:
    """执行预检第 1–5 条（§6.2；任一失败 400，不生成 publish_id）。

    1. 三行存在性 + success（就地复查）+ 新鲜度（rel 严格晚于 json/domain，
       等值按过期——§6.2 等值边界）；
    3. json 画像 ≥1；
    4. domain yaml 核验；
    5. 主配置快照合法（CQLGenerator 构造期完成阈值校验）。
    """
    with platform_session(settings) as session:
        json_marker = _require_step_marker(session, job_uuid, JSON_STEP_FOR_REL)
        domain_marker = _require_step_marker(session, job_uuid, DOMAIN_STEP_FOR_REL)
        rel_marker = _require_step_marker(session, job_uuid, REL_STEP)
        _assert_fresh_ordering(rel_marker, json_marker, domain_marker)

        from backend.data_sources.repository import get_db_source
        from backend.profile_loading.db_loader import (
            MetaLoadSourceDisabledError,
            MetaLoadSourceNotFoundError,
        )

        source = get_db_source(session, source_uuid)
        if source is None:
            raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
        if not source.is_active:
            raise MetaLoadSourceDisabledError("指定数据源已禁用，不能加载画像")
        assert_workspace_owner(session, job_uuid, source_uuid)

        try:
            read_managed_json_input(source_uuid, job_uuid, source.db_name)
        except ManagedJsonInputError as exc:
            raise PublicationStepNotReadyError(
                f"同一作业的 JSON 产物未就绪：{exc}"
            ) from None

        domain_yaml_path = (
            resolve_domain_output_dir(source_uuid, job_uuid) / DOMAIN_ARTIFACT_NAME
        )
        artifact_ok, artifact_reason = verify_domain_artifact(domain_yaml_path)
        if not artifact_ok:
            raise PublicationStepNotReadyError(
                f"{artifact_reason}；请先执行 Domain 或指定已有 job_id"
            )

    # 第 5 条：主配置快照合法（generator 构造期校验 composite_score_threshold）
    from metaweave.core.cql_generator.generator import CQLGenerator

    try:
        main_config = load_main_config_snapshot(resolve_project_path(METADATA_CONFIG))
        CQLGenerator(resolve_project_path(METADATA_CONFIG), config=main_config)
    except ValueError as exc:
        raise PublicationStepNotReadyError(str(exc)) from None


def _rel_artifact_paths(source_uuid: UUID, job_uuid: UUID, database_name: str):
    rel_dir = resolve_rel_output_dir(source_uuid, job_uuid)
    return (
        rel_dir,
        rel_dir / f"{database_name}.relationships_global.json",
        rel_dir / f"{database_name}.relationships_global.md",
    )


def capture_rel_precheck_snapshot(
    settings, source_uuid: UUID, job_uuid: UUID
) -> RelPrecheckSnapshot:
    """第 2+6 条锚点打包：登记快照 + json/domain marker + 文件面 + 指纹。

    文件面核验（第 2 条）：恰两份非忽略普通文件、json 可解析、库名匹配、
    relationships 为列表、statistics 为对象（23.1 采集核验的文件面子集）。
    """
    with platform_session(settings) as session:
        registration = read_registration_snapshot(session, source_uuid)
        json_marker = _require_step_marker(session, job_uuid, JSON_STEP_FOR_REL)
        domain_marker = _require_step_marker(session, job_uuid, DOMAIN_STEP_FOR_REL)
        rel_marker = _require_step_marker(session, job_uuid, REL_STEP)
        # 排序以锚点为准：assert 与 capture 分立会话间的上游重跑由此拦截
        # （见 _assert_fresh_ordering 的并发窗口说明）
        _assert_fresh_ordering(rel_marker, json_marker, domain_marker)

    # 登记快照已校验存在/启用并携带 db_name——不再二次查询源行
    rel_dir, json_path, md_path = _rel_artifact_paths(
        source_uuid, job_uuid, registration.db_name
    )
    actual = set()
    for path in rel_dir.iterdir() if rel_dir.is_dir() else []:
        if path.name.startswith(TMP_PREFIX) or path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            raise PublicationStepNotReadyError(
                f"受管 Rel 目录存在非常规文件: {path.name}"
            )
        actual.add(path.name)
    if actual != {json_path.name, md_path.name}:
        raise PublicationStepNotReadyError(
            "Rel 产物目录应为恰好 json 与 md 两份文件，请重跑 Rel 后再发布"
        )

    status, content = read_regular_text(json_path, "rel json")
    if status != "ok":
        raise PublicationStepNotReadyError("Rel 产物不可读，请重跑 Rel 后再发布")
    try:
        data = json.loads(content)
    except ValueError:
        raise PublicationStepNotReadyError(
            "Rel 产物 JSON 无法解析，请重跑 Rel 后再发布"
        ) from None
    if (
        not isinstance(data, dict)
        or data.get("database") != registration.db_name
        or not isinstance(data.get("relationships"), list)
        or not isinstance(data.get("statistics"), dict)
    ):
        raise PublicationStepNotReadyError(
            "Rel 产物结构不合法（库名/relationships/statistics），请重跑 Rel"
        )

    main_config = load_main_config_snapshot(resolve_project_path(METADATA_CONFIG))
    return RelPrecheckSnapshot(
        registration=registration,
        json_marker=json_marker,
        domain_marker=domain_marker,
        rel_fingerprint=hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
        main_config=main_config,
    )
