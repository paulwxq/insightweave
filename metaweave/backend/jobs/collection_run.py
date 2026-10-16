"""从数据源发起采集作业：执行 db 步骤与受管 ddl 步骤（doc 16 §4）。"""

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from backend.data_sources.db_step_service import DbStepConfigError, run_db_step
from backend.data_sources.ddl_step_service import run_ddl_step
from backend.data_sources.dim_config_step_service import run_dim_config_step
from backend.data_sources.domain_step_service import _UNSET, run_domain_step
from backend.data_sources.json_step_service import run_json_step
from backend.data_sources.md_step_service import run_md_step
from backend.data_sources.rel_step_service import run_rel_step
from backend.data_sources.sql_step_service import run_sql_step
from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
    get_registered_source,
)
from backend.persistence.platform_db import PlatformSettings
from metaweave.utils.time_utils import utc_isoformat
from metaweave.utils.file_utils import resolve_project_path

METADATA_CONFIG = "configs/metadata_config.yaml"
DB_STEP = "db"
DDL_STEP = "ddl"
JSON_STEP = "json"
MD_STEP = "md"
DOMAIN_STEP = "domain"
DIM_CONFIG_STEP = "dim_config"
REL_STEP = "rel"
SQL_STEP = "sql"
_SUPPORTED_STEPS = (
    DB_STEP,
    DDL_STEP,
    JSON_STEP,
    MD_STEP,
    DOMAIN_STEP,
    DIM_CONFIG_STEP,
    REL_STEP,
    SQL_STEP,
)
_SUPPORTED_STEPS_TEXT = "db、ddl、json、md、domain、dim_config、rel、sql"
_UNREGISTERED_FAILURES = ("平台库连接或读取失败", "作业状态登记失败")


class UnsupportedCollectionStepError(ValueError):
    """请求里包含当前还不能执行的步骤，或一个步骤都没选。"""


def normalize_collection_steps(steps: list[str]) -> list[str]:
    if not steps:
        raise UnsupportedCollectionStepError("请至少选择一个步骤")
    chosen: list[str] = []
    for step in steps:
        if step not in _SUPPORTED_STEPS:
            raise UnsupportedCollectionStepError(
                f"当前仅支持 {_SUPPORTED_STEPS_TEXT} 步骤"
            )
        if step not in chosen:
            chosen.append(step)
    return _ensure_managed_step_order(chosen)


# 受管步骤依赖边（doc 19.1 §3、doc 21.1 §6.2、doc 22.1 §6.2、doc 23.1 §6.2）：
# 与既有 _move_after 锚点关系一一对应，外加 json→dim_config 与 json→rel、domain→rel；
# ddl→md 在 json 未选时仍生效。只保留两端都在本次选择内的边。
_STEP_DEPS = {
    JSON_STEP: (DDL_STEP,),
    MD_STEP: (DDL_STEP, JSON_STEP),
    DOMAIN_STEP: (MD_STEP,),
    DIM_CONFIG_STEP: (JSON_STEP,),
    REL_STEP: (JSON_STEP, DOMAIN_STEP),
    SQL_STEP: (MD_STEP, DOMAIN_STEP, REL_STEP),
}


def _ensure_managed_step_order(chosen: list[str]) -> list[str]:
    """同请求偏序（doc 22.1 §6.2，审核修订 3/4）：拓扑排序。

    规则：每轮在当前可执行（有效入度为 0）的步骤中，优先选传入顺序最靠前
    （原始下标最小）者。保证是这条确定性规则而非普遍相对序——阻塞场景下
    "所有无依赖步对保持相对序"成环不可满足（[md, dim_config, ddl] →
    [dim_config, ddl, md] 为既定结果）。对既有五步的链式依赖，与连续
    _move_after 的结果一致（既有排序用例为回归护栏）。
    """
    position = {step: index for index, step in enumerate(chosen)}
    pending = {
        step: {dep for dep in _STEP_DEPS.get(step, ()) if dep in position}
        for step in chosen
    }
    ordered: list[str] = []
    done: set[str] = set()
    while pending:
        available = [step for step in pending if pending[step] <= done]
        nxt = min(available, key=lambda step: position[step])
        ordered.append(nxt)
        done.add(nxt)
        del pending[nxt]
    return ordered


_BLOCK_MESSAGES = {
    (DDL_STEP, JSON_STEP): (
        "本次请求的 DDL 未成功，JSON 未启动；如需使用已有 DDL 产物，请单独重跑 JSON"
    ),
    (JSON_STEP, MD_STEP): (
        "本次请求的 JSON 未成功，MD 未启动；如需使用已有 JSON 产物，请单独重跑 MD"
    ),
    (DDL_STEP, MD_STEP): (
        "本次请求的 DDL 未成功，MD 未启动；如需使用已有 JSON 产物，请单独重跑 MD"
    ),
    (MD_STEP, DOMAIN_STEP): (
        "本次请求的 MD 未成功，Domain 未启动；如需使用已有 MD 产物，请单独重跑 Domain"
    ),
    (JSON_STEP, DIM_CONFIG_STEP): (
        "本次请求的 JSON 未成功，Dim Config 未启动；如需使用已有 JSON 产物，请单独重跑 Dim Config"
    ),
    (JSON_STEP, REL_STEP): (
        "本次请求的 JSON 未成功，Rel 未启动；如需使用已有 JSON 产物，请单独重跑 Rel"
    ),
    (DOMAIN_STEP, REL_STEP): (
        "本次请求的 Domain 未成功，Rel 未启动；如需使用已有 Domain 产物，请单独重跑 Rel"
    ),
    (MD_STEP, SQL_STEP): "本次请求的 MD 未成功，SQL 未启动；请修复 MD 后重跑 SQL",
    (DOMAIN_STEP, SQL_STEP): (
        "本次请求的 Domain 未成功，SQL 未启动；请修复 Domain 后重跑 SQL"
    ),
    (REL_STEP, SQL_STEP): "本次请求的 Rel 未成功，SQL 未启动；请修复 Rel 后重跑 SQL",
}


def _blocked_by_same_request_upstream(
    step: str, chosen: list[str], results: list[dict]
) -> str | None:
    """本次请求内上游失败则阻断后继步骤（doc 19.1 §3）。

    只扫描内存 results 的上游 step/status 条目：上游须是本次选择
    （`chosen`）且已按失败计入结果；MD 在未选 JSON 时改看 DDL。
    """
    failed = {row["step"] for row in results if row["status"] == "failed"}
    pairs = [pair for pair in _BLOCK_MESSAGES if pair[1] == step]
    if step == MD_STEP and JSON_STEP not in chosen:
        pairs = [(DDL_STEP, MD_STEP)]
    elif step == MD_STEP:
        pairs = [(JSON_STEP, MD_STEP)]
    for upstream, _downstream in pairs:
        if upstream in chosen and upstream in failed:
            return _BLOCK_MESSAGES[(upstream, step)]
    return None


def _finished_at() -> str:
    return utc_isoformat(datetime.now(timezone.utc))


def _response_from_step_result(step: str, result: dict) -> dict:
    message = str(result.get("message") or "")
    job_id = result.get("job_id")
    registered = bool(job_id) and not message.startswith(_UNREGISTERED_FAILURES)
    if not result.get("success") and not registered:
        raise PlatformUnavailableError("平台库暂不可用")
    if not job_id:
        raise PlatformUnavailableError("平台库暂不可用")
    return {
        "job_id": str(job_id),
        "results": [
            {
                "step": step,
                "status": "success" if result.get("success") else "failed",
                "message": message,
                "finished_at": _finished_at(),
            }
        ],
    }


def start_db_collection(
    source_id: UUID,
    steps: list[str],
    job_id: UUID | None = None,
    *,
    domain_description=_UNSET,
    sync_domain_description: bool = False,
) -> dict:
    """校验步骤后按选择顺序调用各步骤准备链。

    未传入 job_id 时新建工作区，后续步骤沿同一 job_id 执行；传入时在该
    工作区执行。两种情况都不清理已有产物。各步骤分别记录结果，不把一次
    采集请求视作单个数据库事务（doc 16 §4）。

    `domain_description` / `sync_domain_description`（doc 21.1 §5）：仅 domain
    步骤消费、原样透传给 `run_domain_step`；其余步骤忽略，未包含 domain 时
    不改写任何描述字段。默认哨兵表示本次会话未保存过描述。
    """
    chosen = normalize_collection_steps(steps)
    config_path = resolve_project_path(METADATA_CONFIG)
    results: list[dict] = []
    active_job_id = job_id
    for step in chosen:
        blocked_message = _blocked_by_same_request_upstream(step, chosen, results)
        if blocked_message is not None:
            # 编排层阻断（doc 19.1 §3，含 doc 18 §3.3 修订）：本次上游失败
            # 时后继步骤不执行、不登记 running、不清理旧目录；不得依赖
            # job_info 历史状态推断本次结果。
            results.append(
                {
                    "step": step,
                    "status": "failed",
                    "message": blocked_message,
                    "finished_at": _finished_at(),
                }
            )
            continue
        result = None
        try:
            if step == DB_STEP:
                result = run_db_step(config_path, source_id, False, active_job_id)
            elif step == DDL_STEP:
                result = run_ddl_step(config_path, source_id, active_job_id)
            elif step == JSON_STEP:
                result = run_json_step(config_path, source_id, active_job_id)
            elif step == MD_STEP:
                result = run_md_step(config_path, source_id, active_job_id)
            elif step == DOMAIN_STEP:
                result = run_domain_step(
                    config_path,
                    source_id,
                    active_job_id,
                    domain_description=domain_description,
                    sync_domain_description=sync_domain_description,
                )
            elif step == DIM_CONFIG_STEP:
                result = run_dim_config_step(config_path, source_id, active_job_id)
            elif step == REL_STEP:
                result = run_rel_step(config_path, source_id, active_job_id)
            elif step == SQL_STEP:
                result = run_sql_step(config_path, source_id, active_job_id)
            else:  # normalize 已过滤白名单，防未来新增步骤被静默错路由
                raise UnsupportedCollectionStepError(
                    f"当前仅支持 {_SUPPORTED_STEPS_TEXT} 步骤（收到: {step}）"
                )
            response = _response_from_step_result(step, result)
        except DbStepConfigError as exc:
            if str(exc) == "未找到指定 source_id 的数据源":
                raise SourceNotFoundError("数据源不存在") from None
            if not results:
                # 本次请求还没有任何已执行步骤：整体抛出。不能把请求自带
                # 的 job_id 当作"已有步骤完成"——重跑指定工作区时第一步就
                # 报工作区校验错误，同样没有任何已执行结果可返回。
                raise
            # 已有步骤在本请求内执行并计入结果后，后续步骤报配置错误不再
            # 整体抛出：已完成步骤保持成功，本步骤按配置错误计入结果，
            # 带 job_id 返回部分结果（doc 16 §4：各步骤分别记录，不视作
            # 单个事务）。
            results.append(
                {
                    "step": step,
                    "status": "failed",
                    "message": str(exc),
                    "finished_at": _finished_at(),
                }
            )
            continue
        except PlatformUnavailableError as exc:
            # 正常由 _response_from_step_result 抛出（步骤服务对平台库故障
            # 一律返回结果字典而非抛错）；domain 服务会直接抛其子类
            # DomainPreparationError（本地段非预期故障，doc 21.1 §5.2），
            # 同样按下列分流、由更具体的 API handler 映射 503。
            if not results:
                # 本次请求还没有任何已执行步骤：整体 503（重跑请求同样
                # 不返回部分结果，请求自带的 job_id 不代表步骤已执行）。
                raise
            # 已有步骤在本请求内执行后，后续步骤的平台库故障不再整体
            # 503：该步骤按失败计入结果（其 job_info 多半未能登记，状态
            # 以平台库为准），带 job_id 返回已完成步骤的结果。
            results.append(
                {
                    "step": step,
                    "status": "failed",
                    "message": str((result or {}).get("message") or exc),
                    "finished_at": _finished_at(),
                }
            )
            continue
        if active_job_id is None:
            active_job_id = UUID(response["job_id"])
        results.extend(response["results"])
    return {"job_id": str(active_job_id), "results": results}


async def start_collection_job(
    settings: PlatformSettings,
    source_id: UUID,
    steps: list[str],
    job_id: UUID | None = None,
    *,
    domain_description=_UNSET,
    sync_domain_description: bool = False,
) -> dict:
    """先确认数据源存在，再在线程中执行采集，避免堵住事件循环。

    `domain_description` / `sync_domain_description` 原样透传到
    `start_db_collection`（doc 21.1 §5）。
    """
    normalize_collection_steps(steps)
    get_registered_source(settings, source_id)
    return await asyncio.to_thread(
        start_db_collection,
        source_id,
        steps,
        job_id,
        domain_description=domain_description,
        sync_domain_description=sync_domain_description,
    )
