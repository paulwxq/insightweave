"""删除/清理任务的提交、执行、重试与启动扫描（doc 8.1 §5/§7）。

状态机对应 §5.2/§5.3 的状态表；行锁只用于短事务准入与落库，
外部清理在锁与事务释放后执行，每步另开短事务记录进度。
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from metaweave.core.loaders.cql_loader import normalize_neo4j_section
from metaweave.core.metadata.execution import load_main_config_snapshot
from metaweave.utils.file_utils import resolve_project_path

from backend.access_guards import require_source, require_workspace
from backend.data_sources.repository import purge_source_profiles
from backend.deletion import repository as repo
from backend.deletion import task_lock
from backend.deletion.locks import lock_source_row, lock_workspace_row
from backend.deletion.cleaners import (
    MilvusCleanerOps,
    Neo4jCleanerOps,
    clean_milvus_source,
    clean_neo4j_source,
    clean_source_tree,
    clean_workspace_tree,
)
from backend.deletion.errors import (
    DeletionInProgressError,
    DeletionInvalidRequestError,
    DeletionTargetsChangedError,
    ParentResourceUnavailableError,
    ResourceDeletedError,
    ResourceHasUnfinishedRunsError,
    ResourceNotFoundError,
    TaskPendingClaimError,
    TaskStillRunningError,
)
from backend.deletion.targets import resolve_deletion_targets
from backend.persistence.models import (
    CollectionJobInfo,
    DbSourceInfo,
    PublishJobInfo,
    ResourceDeletionTask,
    WorkspaceInfo,
)
from backend.persistence.platform_db import PlatformSettings, platform_session

METADATA_CONFIG = "configs/metadata_config.yaml"
PENDING_CLAIM_PROTECTION_SECONDS = 15

ACTION_DELETE_SOURCE = "delete_source"
ACTION_DELETE_WORKSPACE = "delete_workspace"
ACTION_CLEAR_KNOWLEDGE = "clear_source_knowledge"

logger = logging.getLogger("backend.deletion")

_STEPS: dict = {}


def _iso(value):
    return value.isoformat() if value is not None else None


def _rows_of(session, model, *conditions) -> list:
    """实体查询统一走 scalars；过滤条件写进 SQL（不做 Python 全表筛）。"""
    stmt = select(model)
    if conditions:
        stmt = stmt.where(*conditions)
    return list(session.scalars(stmt))


def _task_item(task) -> dict:
    return {
        "task_id": str(task.task_id),
        "action": task.action,
        "status": task.status,
        "source_id": task.source_id,
        "workspace_id": task.workspace_id,
        "target_id": task.target_id,
        "error_message": task.error_message,
        "created_at": _iso(task.created_at),
        "queued_at": _iso(task.queued_at),
        "started_at": _iso(task.started_at),
        "finished_at": _iso(task.finished_at),
    }


def _is_deleted(row) -> bool:
    return getattr(row, "deleted_at", None) is not None or (
        getattr(row, "deletion_state", "active") == "deleted"
    )


def _serialize_runs(runs: list[dict]) -> list[dict]:
    serialized = []
    for run in runs:
        item = dict(run)
        item["run_id"] = str(run["run_id"])
        item["created_at"] = _iso(run.get("created_at"))
        item["started_at"] = _iso(run.get("started_at"))
        serialized.append(item)
    return serialized


def _admission_check(
    session,
    *,
    action: str,
    target_id: str,
    source_id: str,
    workspace_id: str | None,
):
    """同源任务互斥 + 未结束运行准入；返回已有任务或 None（§5.1/§7.1）。"""
    existing = repo.unresolved_task_for_source(session, source_id)
    if existing is not None:
        if existing.action == action and existing.target_id == target_id:
            return existing
        raise DeletionInProgressError(
            "该数据源已有删除或清理任务，仅可重试原任务",
            payload={"task_id": str(existing.task_id)},
        )
    if workspace_id is not None:
        blocking = repo.unfinished_runs_in_scope(session, workspace_id=workspace_id)
    else:
        blocking = repo.unfinished_runs_in_scope(session, source_id=source_id)
    if blocking:
        raise ResourceHasUnfinishedRunsError(
            "范围内存在未结束运行，不能删除或清理",
            payload={"blocking_runs": _serialize_runs(blocking)},
        )
    return None


def _insert_task_with_mutex_guard(session, **kwargs) -> object:
    """插入任务；部分唯一索引冲突时回滚后查询冲突任务（§7.1 提交）。"""
    source_id = kwargs["source_id"]
    try:
        return repo.insert_task(session, **kwargs)
    except IntegrityError:
        session.rollback()
        conflict = repo.unresolved_task_for_source(session, source_id)
        if conflict is not None:
            raise DeletionInProgressError(
                "该数据源已有删除或清理任务",
                payload={"task_id": str(conflict.task_id)},
            ) from None
        raise


def submit_source_deletion(settings: PlatformSettings, source_id: str) -> dict:
    with platform_session(settings) as session:
        source = lock_source_row(session, source_id)
        if source is None:
            raise ResourceNotFoundError("数据源不存在")
        if _is_deleted(source):
            raise ResourceDeletedError("数据源已删除")
        existing = _admission_check(
            session,
            action=ACTION_DELETE_SOURCE,
            target_id=source_id,
            source_id=source_id,
            workspace_id=None,
        )
        if existing is not None:
            return _task_item(existing)
        source.deletion_state = "deleting"
        task = _insert_task_with_mutex_guard(
            session,
            action=ACTION_DELETE_SOURCE,
            target_id=source_id,
            source_id=source_id,
        )
        return _task_item(task)


def submit_workspace_deletion(settings: PlatformSettings, workspace_id: str) -> dict:
    with platform_session(settings) as session:
        probe = session.get(WorkspaceInfo, workspace_id)
        if probe is None:
            raise ResourceNotFoundError("工作区不存在")
        # §5.1.1：普通查询只用于定位父级，随后从源开始加锁并重新校验
        source = lock_source_row(session, probe.source_id)
        workspace = lock_workspace_row(session, workspace_id)
        if workspace is None:
            raise ResourceNotFoundError("工作区不存在")
        if source is None or source.source_id != workspace.source_id:
            raise ResourceNotFoundError("数据源不存在")
        if _is_deleted(workspace):
            raise ResourceDeletedError("工作区已删除")
        if _is_deleted(source):
            raise ResourceDeletedError("数据源已删除")
        if source.deletion_state in ("deleting", "delete_failed"):
            raise ParentResourceUnavailableError(
                "数据源正在删除或删除失败，不能删除其工作区",
                payload={"parent_type": "source", "source_id": source.source_id},
            )
        existing = _admission_check(
            session,
            action=ACTION_DELETE_WORKSPACE,
            target_id=workspace_id,
            source_id=source.source_id,
            workspace_id=workspace_id,
        )
        if existing is not None:
            return _task_item(existing)
        workspace.deletion_state = "deleting"
        task = _insert_task_with_mutex_guard(
            session,
            action=ACTION_DELETE_WORKSPACE,
            target_id=workspace_id,
            source_id=source.source_id,
            workspace_id=workspace_id,
        )
        return _task_item(task)


def submit_knowledge_clear(settings: PlatformSettings, source_id: str) -> dict:
    with platform_session(settings) as session:
        source = lock_source_row(session, source_id)
        if source is None:
            raise ResourceNotFoundError("数据源不存在")
        if _is_deleted(source):
            raise ResourceDeletedError("数据源已删除")
        existing = _admission_check(
            session,
            action=ACTION_CLEAR_KNOWLEDGE,
            target_id=source_id,
            source_id=source_id,
            workspace_id=None,
        )
        if existing is not None:
            return _task_item(existing)
        # 独立知识清理保持源 active（§5.3），冻结由任务状态表达
        task = _insert_task_with_mutex_guard(
            session,
            action=ACTION_CLEAR_KNOWLEDGE,
            target_id=source_id,
            source_id=source_id,
        )
        return _task_item(task)


# ---- 执行 ----


def default_deps(main_config: dict) -> dict:
    """生产清理依赖：连接一律使用本次执行读取的同一份配置快照。

    doc 8.1 §7（审查收紧）：指纹校验与实际清理连接不得各自读配置——
    否则两次读取之间配置变化会“检查 A、连接 B”。工厂闭包持有
    ``_execute_locked`` 传入的快照，不再读 YAML；密码只存在于本次
    执行内存，不写入任务表或日志。
    """

    def milvus_ops_factory():
        providers = main_config["vector_database"]["providers"]["milvus"]
        return MilvusCleanerOps(providers)

    def neo4j_ops_factory():
        section = normalize_neo4j_section(main_config["loaders"]["cql_loader"]["neo4j"])
        return Neo4jCleanerOps(section)

    return {
        "milvus_ops_factory": milvus_ops_factory,
        "neo4j_ops_factory": neo4j_ops_factory,
        "pg_purge": purge_source_profiles,
        "workspace_tree": clean_workspace_tree,
        "source_tree": clean_source_tree,
    }


def _step_milvus(settings, deps, task, targets) -> dict:
    ops = deps["milvus_ops_factory"]()
    try:
        return clean_milvus_source(
            ops, task.source_id, targets["milvus"]["collections"]
        )
    finally:
        ops.close()


def _step_neo4j(settings, deps, task, targets) -> dict:
    ops = deps["neo4j_ops_factory"]()
    # doc 10.2 §5.1：每次目标删除/查询请求前后探测目标锁连接（deps
    # 注入，由 _execute_locked 在持有目标互斥时写入；替身缺省为空）
    lock_probe = (deps or {}).get("neo4j_lock_probe")
    try:
        return clean_neo4j_source(ops, task.source_id, lock_probe=lock_probe)
    finally:
        ops.close()


def _step_pg(settings, deps, task, targets) -> dict:
    with platform_session(settings) as session:
        result = deps["pg_purge"](session, task.source_id)
        session.commit()
    return result or {}


def _step_files(settings, deps, task, targets) -> dict:
    if task.action == ACTION_DELETE_SOURCE:
        return deps["source_tree"](task.source_id)
    return deps["workspace_tree"](task.source_id, task.workspace_id)


def _steps_for(action: str) -> list:
    if action == ACTION_DELETE_SOURCE:
        return [
            ("milvus", _step_milvus),
            ("neo4j", _step_neo4j),
            ("pg", _step_pg),
            ("files", _step_files),
        ]
    if action == ACTION_CLEAR_KNOWLEDGE:
        return [
            ("milvus", _step_milvus),
            ("neo4j", _step_neo4j),
            ("pg", _step_pg),
        ]
    return [("files", _step_files)]


def execute_task(
    settings: PlatformSettings, task_id: UUID | str, *, deps: dict | None = None
) -> dict:
    task_id = UUID(str(task_id))
    key = task_lock.task_lock_key(task_id)
    conn = task_lock.connect_task_lock(settings)
    if not task_lock.try_acquire(conn, key):
        task_lock.close(conn)
        return {"task_id": str(task_id), "claimed": False}

    def _ensure_lock() -> None:
        """同一专用连接上复检锁仍持有；失锁抛 TaskLockLostError（§7.1）。"""
        task_lock.ensure_lock_connection_alive(conn)

    try:
        # deps 延迟到配置读取后创建：连接与指纹共用同一份快照
        return _execute_locked(settings, task_id, deps, ensure_lock=_ensure_lock)
    except task_lock.TaskLockLostError as exc:
        # 失锁：不改写任务状态（其他执行者可能已接管），只记日志；
        # 任务由既有启动扫描/重试流程收尾（§7.1/§7.2）
        logger.error(
            "删除任务执行锁连接丢失，放弃后续清理 run_id=%s reason=%s",
            task_id,
            exc,
        )
        return {"task_id": str(task_id), "status": "lock_lost", "error": str(exc)}
    finally:
        try:
            task_lock.release(conn, key)
        except Exception:  # noqa: BLE001 —— 清理异常不得覆盖原始失锁原因
            logger.error("任务执行锁释放失败（连接已按失效处理）run_id=%s", task_id)


class _TaskRef:
    """携带任务标识穿过外部步骤（不持有会话与行锁）。"""

    def __init__(self, task):
        self.action = task.action
        self.source_id = task.source_id
        self.workspace_id = task.workspace_id


def _parent_state_matches(task, source, workspace) -> str | None:
    if task.action == ACTION_DELETE_SOURCE:
        if source is None:
            return "源不存在，任务失败"
        if source.deletion_state != "deleting":
            return "源状态与任务不一致"
    elif task.action == ACTION_DELETE_WORKSPACE:
        if workspace is None:
            return "工作区不存在，任务失败"
        if workspace.deletion_state != "deleting":
            return "工作区状态与任务不一致"
    else:
        if source is None:
            return "源不存在，任务失败"
        if source.deletion_state != "active":
            return "源状态与任务不一致"
    return None


def _execute_locked(
    settings: PlatformSettings,
    task_id: UUID,
    deps: dict | None,
    *,
    ensure_lock=None,
) -> dict:
    with platform_session(settings) as session:
        task = repo.get_task(session, task_id)
        if task is None or task.status != "pending":
            return {"task_id": str(task_id), "claimed": False}
        source = lock_source_row(session, task.source_id)
        workspace = None
        if task.action == ACTION_DELETE_WORKSPACE:
            workspace = lock_workspace_row(session, task.workspace_id)
        mismatch = _parent_state_matches(task, source, workspace)
        if mismatch is not None:
            repo.mark_task_failed(session, task_id, error_message=mismatch)
            session.commit()
            return {"task_id": str(task_id), "status": "failed", "error": mismatch}
        if not repo.claim_task(session, task_id):
            return {"task_id": str(task_id), "claimed": False}
        session.commit()
        ref = _TaskRef(task)

    def fail_with_lock_check(step, exc):
        # §7.1：所有失败收尾在写状态前复检锁连接；失锁抛 TaskLockLostError，
        # 不覆盖可能已接管任务的其他执行者写下的状态
        if ensure_lock is not None:
            ensure_lock()
        return _fail(settings, task_id, ref=ref, step=step, exc=exc)

    if ref.action == ACTION_DELETE_WORKSPACE:
        # 工作区删除只处理目录（resolve_workspace_root 受管解析，不信客户端
        # 路径）：不读取 metadata_config.yaml，不解析/比对 RAG 目标指纹——
        # 图配置缺失不得阻止纯目录删除，RAG 配置变更也不得拒绝其重试
        targets = None
        # 生产默认提供目录清理依赖（可被测试注入同名键覆盖）；
        # 仍不读 metadata_config.yaml、不建 Milvus/Neo4j 连接
        deps = {"workspace_tree": clean_workspace_tree, **(deps or {})}
    else:
        # 目标配置指纹：本次执行只读一次配置，指纹与连接共用这份快照（§7；
        # 重试时重新读取并与原目标核对，变化即拒绝、不产生删除副作用）
        try:
            main_config = load_main_config_snapshot(
                resolve_project_path(METADATA_CONFIG)
            )
            targets = resolve_deletion_targets(main_config)
        except ValueError as exc:
            return fail_with_lock_check("targets", exc)
        deps = deps or default_deps(main_config)

        with platform_session(settings) as session:
            task = repo.get_task(session, task_id)
            stored = (task.step_results or {}).get("_targets")
            if stored and stored.get("fingerprint") != targets["fingerprint"]:
                error = "目标存储配置已变化，拒绝继续清理（请人工核对后再重试）"
                return fail_with_lock_check("targets", RuntimeError(error))
            if not stored:
                repo.record_step(
                    session,
                    task_id,
                    "_targets",
                    {
                        "fingerprint": targets["fingerprint"],
                        "milvus": targets["milvus"],
                        "neo4j": targets["neo4j"],
                    },
                )

    # doc 10.2 §5.1：删除与发布共享 Neo4j 目标互斥协议——任务 advisory 锁
    # 之后、破坏性清理步骤之前取得部署级目标 advisory lock 并建立持久保留
    # （claim 事务已提交，不与源/工作区行锁嵌套）。遗留保留记录（含同
    # task_id 的重试重入）一律拒绝进入，人工核验清除后才能重试。
    target_mutex = None
    if ref.action != ACTION_DELETE_WORKSPACE:
        # deps 可注入替身（测试不建真连接，返回 None 表示不建模目标互斥）；
        # 生产缺省走真实目标互斥（doc 10.2 §5.1）。
        enter_target = (deps or {}).get("neo4j_target_enter") or (
            _enter_neo4j_target_mutex
        )
        target_mutex = enter_target(settings, targets, task_id)
        if isinstance(target_mutex, Exception):
            return fail_with_lock_check("neo4j_target", target_mutex)

    from backend.publishing.target_lock import (
        TargetLockLostError,
        ensure_target_lock_alive,
        release_target_lock,
    )

    terminal_recorded = False
    task_succeeded = False
    if target_mutex is not None:
        # 每次目标请求边界探测锁连接（doc 10.2 §5.1）；失锁抛
        # TargetLockLostError → 转任务锁失锁语义（不改写终态、保留记录）
        deps["neo4j_lock_probe"] = lambda: ensure_target_lock_alive(
            target_mutex.conn
        )
    try:
        for step_name, step_fn in _steps_for(ref.action):
            # 每个破坏性步骤前后复检锁连接（§7.1：失锁停止后续动作，不承诺恰好一次）
            if ensure_lock is not None:
                ensure_lock()
            if target_mutex is not None:
                try:
                    ensure_target_lock_alive(target_mutex.conn)
                except TargetLockLostError as exc:
                    raise task_lock.TaskLockLostError(str(exc)) from exc
            try:
                result = step_fn(settings, deps, ref, targets)
            except task_lock.TaskLockLostError:
                raise
            except TargetLockLostError as exc:
                # 目标锁失锁与任务锁失锁同语义：不改写任务终态，保留记录
                raise task_lock.TaskLockLostError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 —— 步骤失败转为任务 failed
                outcome = fail_with_lock_check(step_name, exc)
                terminal_recorded = True
                return outcome
            if ensure_lock is not None:
                ensure_lock()
            if target_mutex is not None:
                try:
                    ensure_target_lock_alive(target_mutex.conn)
                except TargetLockLostError as exc:
                    raise task_lock.TaskLockLostError(str(exc)) from exc
            with platform_session(settings) as session:
                repo.record_step(session, task_id, step_name, result)
                session.commit()

        with platform_session(settings) as session:
            lock_source_row(session, ref.source_id)
            if ref.action == ACTION_DELETE_WORKSPACE:
                lock_workspace_row(session, ref.workspace_id)
            # 最终标记对象已删除前复检锁连接（§7.1）
            if ensure_lock is not None:
                ensure_lock()
            _finalize_rows(session, ref)
            repo.mark_task_success(session, task_id)
            session.commit()
        terminal_recorded = True
        task_succeeded = True
        return {"task_id": str(task_id), "status": "success"}
    finally:
        if target_mutex is not None:
            if terminal_recorded and task_succeeded:
                # 任务成功（全部清理步骤完成且终态可靠提交）：按 owner
                # 匹配清除保留记录后释放锁。失败一律保留——失败路径的
                # 外部请求结果无法逐一确认（网络中断时服务端可能仍在
                # 执行），重试由遗留保留记录挡住、人工核验后清除
                # （doc 10.2 §5.1，Codex R1 意见 3）。
                _exit_neo4j_target_mutex(settings, target_mutex)
            else:
                # 未达终态或失败：保留记录，只关闭锁连接；释放失败不得
                # 遮蔽正在传播的失锁主异常（Codex R2 意见 4 同源）
                try:
                    release_target_lock(target_mutex.conn, target_mutex.key)
                except Exception:  # noqa: BLE001
                    logger.exception("释放 Neo4j 目标锁失败（不遮蔽主异常）")


class _Neo4jTargetMutex:
    """Neo4j 目标互斥句柄（doc 10.2 §5.1，删除侧按任务粒度持有）。"""

    def __init__(self, conn, key: int, target_key: str, owner_id: str):
        self.conn = conn
        self.key = key
        self.target_key = target_key
        self.owner_id = owner_id


def _enter_neo4j_target_mutex(settings: PlatformSettings, targets, task_id):
    """取得 Neo4j 目标锁并建立持久保留；失败返回异常对象（不抛出）。

    成功返回 ``_Neo4jTargetMutex``。保留记录已存在（任何 owner，含同
    task_id 重试重入）时拒绝进入——重新取得 advisory lock 不等于有权
    沿用历史记录（doc 10.2 §5.1）。
    """
    from backend.publishing.scheduling import publish_lock_timeout_ms
    from backend.publishing.target_lock import (
        TargetLockTimeoutError,
        acquire_target_lock,
        neo4j_target_guard_key,
        neo4j_target_lock_key,
        release_target_lock,
        reserve_target_guard,
    )

    neo = (targets or {}).get("neo4j") or {}
    uri = str(neo.get("uri") or "")
    database = str(neo.get("database") or "neo4j")
    # 可读键与锁键同一规范化口径（主机+端口+database；协议/别名写法
    # 不区分实例，2026-11-06 堵别名坑）
    target_key = neo4j_target_guard_key(uri, database)
    key = neo4j_target_lock_key(uri, database)
    try:
        conn = acquire_target_lock(
            settings, key, wait_timeout_s=publish_lock_timeout_ms() / 1000
        )
    except TargetLockTimeoutError as exc:
        return exc
    except Exception as exc:  # noqa: BLE001 —— 连接级失败也受控转任务 failed，
        # 不从 _execute_locked 逃逸遗留 running（Kimi R1 意见 4）
        return exc
    existing = None
    try:
        with platform_session(settings) as session:
            existing = reserve_target_guard(
                session, target_key, "deletion_task", str(task_id)
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001 —— 保留记录建立失败：释放锁并转为任务失败
        release_target_lock(conn, key)
        return exc
    if existing is not None:
        release_target_lock(conn, key)
        return RuntimeError(
            "Neo4j 目标保留记录存在（"
            f"owner={existing['owner_kind']}:{existing['owner_id']}），"
            "上次执行中断且结果未核验，请人工核验清除后重试"
        )
    return _Neo4jTargetMutex(
        conn=conn, key=key, target_key=target_key, owner_id=str(task_id)
    )


def _exit_neo4j_target_mutex(settings: PlatformSettings, mutex) -> None:
    """任务终态可靠提交后按 owner 匹配清除保留记录并释放目标锁。"""
    from backend.publishing.target_lock import (
        release_target_guard,
        release_target_lock,
    )

    try:
        with platform_session(settings) as session:
            release_target_guard(
                session, mutex.target_key, "deletion_task", mutex.owner_id
            )
            session.commit()
    except Exception:  # noqa: BLE001 —— 清除失败：记录保留待人工核验
        logger.exception(
            "清除 Neo4j 目标保留记录失败（需人工核验）task=%s", mutex.owner_id
        )
    finally:
        release_target_lock(mutex.conn, mutex.key)


def _fail(settings, task_id, *, ref: _TaskRef, step, exc) -> dict:
    with platform_session(settings) as session:
        source = lock_source_row(session, ref.source_id)
        workspace = None
        if ref.action == ACTION_DELETE_WORKSPACE and ref.workspace_id:
            workspace = lock_workspace_row(session, ref.workspace_id)
        repo.mark_task_failed(session, task_id, error_message=f"{step} 清理失败：{exc}")
        if ref.action == ACTION_DELETE_SOURCE and source is not None:
            source.deletion_state = "delete_failed"
        elif ref.action == ACTION_DELETE_WORKSPACE and workspace is not None:
            workspace.deletion_state = "delete_failed"
        session.commit()
    return {
        "task_id": str(task_id),
        "status": "failed",
        "step": step,
        "error": str(exc),
    }


def _finalize_rows(session, ref: _TaskRef) -> None:
    now = repo.db_now(session)
    if ref.action == ACTION_DELETE_SOURCE:
        source = session.get(DbSourceInfo, ref.source_id)
        if source is not None and not _is_deleted(source):
            # §3.5 字段清单：清连接字段，保留最小登记
            source.deleted_at = now
            source.deletion_state = "deleted"
            source.description = None
            source.host = None
            source.port = None
            source.db_name = None
            source.username = None
            source.password_ciphertext = None
            source.sslmode = None
            source.db_version = None
            source.selected_schemas = None
            source.is_active = False
            source.updated_at = now
        ws_ids = set(
            session.scalars(
                select(WorkspaceInfo.workspace_id).where(
                    WorkspaceInfo.source_id == ref.source_id
                )
            )
        )
        for workspace in _rows_of(
            session, WorkspaceInfo, WorkspaceInfo.source_id == ref.source_id
        ):
            if _is_deleted(workspace):
                continue
            workspace.deleted_at = now
            workspace.deletion_state = "deleted"
        for model in (CollectionJobInfo, PublishJobInfo):
            id_column = model.workspace_id
            for job in _rows_of(session, model, id_column.in_(ws_ids)):
                if job.deleted_at is None:
                    job.deleted_at = now
    elif ref.action == ACTION_DELETE_WORKSPACE:
        workspace = session.get(WorkspaceInfo, ref.workspace_id)
        if workspace is not None and not _is_deleted(workspace):
            workspace.deleted_at = now
            workspace.deletion_state = "deleted"
        for model in (CollectionJobInfo, PublishJobInfo):
            for job in _rows_of(session, model, model.workspace_id == ref.workspace_id):
                if job.deleted_at is None:
                    job.deleted_at = now


# ---- 重试与启动扫描 ----


def _confirm_retry_targets(task, *, confirmation: dict | None, now) -> None:
    """在任务锁和源锁内核对用户看到的旧/新目标，确认后原子更新。

    确认绑定两个指纹；对话框打开后配置或任务再次变化时重新确认。
    新目标从服务端配置解析，不接受客户端传入连接地址。旧步骤归档，
    新目标从 Milvus 开始重新清理，不能把旧目标的成功当成新目标成功。
    """
    if task.action == ACTION_DELETE_WORKSPACE:
        if confirmation is not None:
            raise DeletionInvalidRequestError("工作区删除无需确认存储目标")
        return
    steps = dict(task.step_results or {})
    previous = steps.get("_targets")
    if not previous:
        if confirmation is not None:
            raise DeletionInvalidRequestError("任务目标确认已失效，请重新获取任务")
        return
    try:
        current = resolve_deletion_targets(
            load_main_config_snapshot(resolve_project_path(METADATA_CONFIG))
        )
    except ValueError as exc:
        raise DeletionInvalidRequestError(str(exc)) from exc
    expected = {
        "previous_fingerprint": previous["fingerprint"],
        "current_fingerprint": current["fingerprint"],
    }
    changed = previous["fingerprint"] != current["fingerprint"]
    if (changed or confirmation is not None) and confirmation != expected:
        raise DeletionTargetsChangedError(
            "存储目标已变化，请核对原目标和当前目标，确认后继续",
            payload={"previous": previous, "current": current},
        )
    if not changed:
        return
    history = list(steps.get("_target_changes") or [])
    history.append(
        {
            "confirmed_at": now.isoformat(),
            "previous": previous,
            "current": current,
            "previous_steps": {
                key: value for key, value in steps.items() if not key.startswith("_")
            },
        }
    )
    task.step_results = {"_targets": current, "_target_changes": history}


def retry_task(
    settings: PlatformSettings,
    task_id: UUID | str,
    *,
    target_confirmation: dict | None = None,
) -> dict:
    task_id = UUID(str(task_id))
    key = task_lock.task_lock_key(task_id)
    conn = task_lock.connect_task_lock(settings)
    if not task_lock.try_acquire(conn, key):
        task_lock.close(conn)
        raise TaskStillRunningError("任务执行锁仍被持有，不能重试")
    try:
        with platform_session(settings) as session:
            task = repo.get_task(session, task_id)
            if task is None:
                raise ResourceNotFoundError("删除任务不存在")
            source = lock_source_row(session, task.source_id)
            workspace = None
            if task.action == ACTION_DELETE_WORKSPACE:
                workspace = lock_workspace_row(session, task.workspace_id)
            now = repo.db_now(session)
            task = repo.get_task(session, task_id)
            if task.status == "success":
                raise DeletionInvalidRequestError("任务已成功，无需重试")
            if task.status == "pending":
                age = (now - task.queued_at).total_seconds()
                if age < PENDING_CLAIM_PROTECTION_SECONDS:
                    raise TaskPendingClaimError(
                        "任务尚在认领保护期内（15 秒），请稍后重试"
                    )
            # 未确认时在状态迁移/重新入队前返回 409，不派发后台清理。
            _confirm_retry_targets(task, confirmation=target_confirmation, now=now)
            task_lock.ensure_lock_connection_alive(conn)
            if task.status == "pending":
                repo.interrupt_pending_task(
                    session, task_id, reason="认领等待超时，确认中断后重新入队"
                )
            elif task.status == "running":
                repo.interrupt_task(
                    session, task_id, reason="执行已中断（重试确认），等待重新入队"
                )
            if not repo.retry_transition(session, task_id):
                raise DeletionInProgressError("任务状态已变化，请刷新后重试")
            if task.action == ACTION_DELETE_SOURCE and source is not None:
                source.deletion_state = "deleting"
            elif task.action == ACTION_DELETE_WORKSPACE and workspace is not None:
                workspace.deletion_state = "deleting"
            session.commit()
            task = repo.get_task(session, task_id)
            return _task_item(task)
    finally:
        task_lock.release(conn, key)


def _mark_interrupted_object(session, task, source) -> None:
    if task.action == ACTION_DELETE_SOURCE and source is not None:
        source.deletion_state = "delete_failed"
    elif task.action == ACTION_DELETE_WORKSPACE and task.workspace_id:
        workspace = lock_workspace_row(session, task.workspace_id)
        if workspace is not None:
            workspace.deletion_state = "delete_failed"
    # clear_source_knowledge：源保持 active（§5.3 状态表）


def startup_scan(settings: PlatformSettings) -> dict:
    """启动恢复扫描：只修复状态，不自动执行清理（§7.2）。"""
    with platform_session(settings) as session:
        candidates = _rows_of(
            session,
            ResourceDeletionTask,
            ResourceDeletionTask.status.in_(("running", "pending")),
        )
    interrupted = 0
    for task in candidates:
        key = task_lock.task_lock_key(task.task_id)
        conn = task_lock.connect_task_lock(settings)
        if not task_lock.try_acquire(conn, key):
            task_lock.close(conn)
            continue
        try:
            with platform_session(settings) as session:
                source = lock_source_row(session, task.source_id)
                fresh = repo.get_task(session, task.task_id)
                if fresh is None:
                    continue
                now = repo.db_now(session)
                if fresh.status == "running":
                    repo.interrupt_task(
                        session, fresh.task_id, reason="执行已中断，等待人工重试"
                    )
                    _mark_interrupted_object(session, fresh, source)
                    session.commit()
                    interrupted += 1
                elif (
                    fresh.status == "pending"
                    and (now - fresh.queued_at).total_seconds()
                    >= PENDING_CLAIM_PROTECTION_SECONDS
                ):
                    repo.interrupt_pending_task(
                        session, fresh.task_id, reason="认领等待超时，可重新派发"
                    )
                    _mark_interrupted_object(session, fresh, source)
                    session.commit()
                    interrupted += 1
        finally:
            task_lock.release(conn, key)
    return {"interrupted": interrupted}


# ---- 详情与预览 ----


def task_detail(settings: PlatformSettings, task_id: UUID | str) -> dict:
    task_id = UUID(str(task_id))
    with platform_session(settings) as session:
        task = repo.get_task(session, task_id)
        if task is None:
            raise ResourceNotFoundError("删除任务不存在")
        now = repo.db_now(session)
        item = _task_item(task)
        status = task.status
        queued_at = task.queued_at
        step_results = task.step_results or {}

    def _probe() -> bool:
        key = task_lock.task_lock_key(task_id)
        conn = task_lock.connect_task_lock(settings)
        if task_lock.try_acquire(conn, key):
            task_lock.release(conn, key)
            return True
        task_lock.close(conn)
        return False

    if status == "failed":
        can_retry, reason = True, "可重试"
    elif status == "success":
        can_retry, reason = False, "已完成"
    elif (
        status == "pending"
        and (now - queued_at).total_seconds() < PENDING_CLAIM_PROTECTION_SECONDS
    ):
        can_retry, reason = False, "正在排队"
    elif status == "pending":
        can_retry = _probe()
        reason = "认领等待超时，检查并重试" if can_retry else "正在认领"
    else:  # running
        can_retry = _probe()
        reason = "检查并重试" if can_retry else "仍在执行"
    item.update(can_retry=can_retry, retry_reason=reason, steps=step_results)
    return item


def _resolve_targets_view() -> dict:
    try:
        targets = resolve_deletion_targets(
            load_main_config_snapshot(resolve_project_path(METADATA_CONFIG))
        )
        return {"milvus": targets["milvus"], "neo4j": targets["neo4j"]}
    except ValueError as exc:
        return {"error": str(exc)}


def preview_source_deletion(settings: PlatformSettings, source_id: str) -> dict:
    with platform_session(settings) as session:
        require_source(session, source_id)
        workspaces = _rows_of(
            session,
            WorkspaceInfo,
            WorkspaceInfo.source_id == source_id,
            WorkspaceInfo.deleted_at.is_(None),
        )
        ws_ids = [w.workspace_id for w in workspaces]
        collection_jobs = _rows_of(
            session,
            CollectionJobInfo,
            CollectionJobInfo.workspace_id.in_(ws_ids),
            CollectionJobInfo.deleted_at.is_(None),
        )
        publish_jobs = _rows_of(
            session,
            PublishJobInfo,
            PublishJobInfo.workspace_id.in_(ws_ids),
            PublishJobInfo.deleted_at.is_(None),
        )
        blocking = repo.unfinished_runs_in_scope(session, source_id=source_id)
    return {
        "source_id": source_id,
        "workspace_count": len(workspaces),
        "collection_job_count": len(collection_jobs),
        "publish_job_count": len(publish_jobs),
        "blocking_runs": _serialize_runs(blocking),
        "targets": _resolve_targets_view(),
    }


def preview_workspace_deletion(settings: PlatformSettings, workspace_id: str) -> dict:
    with platform_session(settings) as session:
        workspace = require_workspace(session, workspace_id)
        collection_jobs = _rows_of(
            session,
            CollectionJobInfo,
            CollectionJobInfo.workspace_id == workspace_id,
            CollectionJobInfo.deleted_at.is_(None),
        )
        publish_jobs = _rows_of(
            session,
            PublishJobInfo,
            PublishJobInfo.workspace_id == workspace_id,
            PublishJobInfo.deleted_at.is_(None),
        )
        blocking = repo.unfinished_runs_in_scope(session, workspace_id=workspace_id)
        source_id = workspace.source_id
    return {
        "workspace_id": workspace_id,
        "source_id": source_id,
        "collection_job_count": len(collection_jobs),
        "publish_job_count": len(publish_jobs),
        "blocking_runs": _serialize_runs(blocking),
    }
