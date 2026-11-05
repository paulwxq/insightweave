"""删除/清理链路的稳定错误（doc 8.1 §7.3）。

服务层抛出，路由层映射 HTTP 与错误码；payload 附加冲突任务、阻断运行等。
"""

from __future__ import annotations


class DeletionError(Exception):
    """基类：code 为稳定错误码，http_status 由路由层使用。"""

    code = "deletion_error"
    http_status = 409

    def __init__(self, message: str, *, payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}


class ResourceNotFoundError(DeletionError):
    code = "resource_not_found"
    http_status = 404


class ResourceDeletedError(DeletionError):
    code = "resource_deleted"
    http_status = 410


class ResourceHasUnfinishedRunsError(DeletionError):
    code = "resource_has_unfinished_runs"
    http_status = 409


class ParentResourceUnavailableError(DeletionError):
    code = "parent_resource_unavailable"
    http_status = 409


class SourceKnowledgeFrozenError(DeletionError):
    code = "source_knowledge_frozen"
    http_status = 409


class DeletionInProgressError(DeletionError):
    code = "deletion_in_progress"
    http_status = 409


class TaskPendingClaimError(DeletionError):
    code = "task_pending_claim"
    http_status = 409


class TaskStillRunningError(DeletionError):
    code = "task_still_running"
    http_status = 409


class DeletionTargetsChangedError(DeletionError):
    code = "deletion_targets_changed"
    http_status = 409


class DeletionInvalidRequestError(DeletionError):
    code = "invalid_request"
    http_status = 400


class DeletionDependencyUnavailableError(DeletionError):
    code = "deletion_dependency_unavailable"
    http_status = 503


class DeletionStepError(Exception):
    """清理步骤失败（执行器内部使用；转为任务 failed，不是 HTTP 错误）。"""
