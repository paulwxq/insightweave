"""Web API v2 错误码闭集与稳定异常（doc 2 §4.2）。

非 200 外层 data.error_code 的全集即 ERROR_CODES；HTTP 200 的
WebQueryResult.error_code 仅为 TERMINAL_ERROR_CODES 加 null。错误字符串
不能由异常类名/中文消息临时生成，新增值须同步契约与测试。
"""

from __future__ import annotations

from typing import Any, Optional

# 非 200 响应的稳定 error_code 闭集（doc 2 §4.2 错误映射表）
INVALID_REQUEST = "invalid_request"
SESSION_NOT_FOUND = "session_not_found"
SOURCE_NOT_FOUND = "source_not_found"
RESULT_NOT_FOUND = "result_not_found"
ROUTE_NOT_FOUND = "route_not_found"
METHOD_NOT_ALLOWED = "method_not_allowed"
SESSION_BUSY = "session_busy"
REQUEST_ID_CONFLICT = "request_id_conflict"
SESSION_METADATA_INVALID = "session_metadata_invalid"
SOURCE_INACTIVE = "source_inactive"
SOURCE_CHANGED = "source_changed"
SOURCE_MISMATCH = "source_mismatch"
SESSION_BINDING_MISSING = "session_binding_missing"
KNOWLEDGE_CHANGED = "knowledge_changed"
SESSION_INVALIDATED = "session_invalidated"
SOURCE_NOT_READY = "source_not_ready"
SOURCE_UNSUPPORTED = "source_unsupported"
SOURCE_CONFIG_INVALID = "source_config_invalid"
ORIGIN_NOT_ALLOWED = "origin_not_allowed"
HOST_NOT_ALLOWED = "host_not_allowed"
UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
QUERY_CAPACITY_EXCEEDED = "query_capacity_exceeded"
RATE_LIMITED = "rate_limited"
LOCK_BACKEND_ERROR = "lock_backend_error"
SOURCE_REGISTRY_UNAVAILABLE = "source_registry_unavailable"
DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
HISTORY_UNAVAILABLE = "history_unavailable"
HISTORY_RECORD_INVALID = "history_record_invalid"
RESULT_INTEGRITY_ERROR = "result_integrity_error"
RESULT_ENCODING_ERROR = "result_encoding_error"
INTERNAL_ERROR = "internal_error"

ERROR_CODES = frozenset(
    {
        INVALID_REQUEST,
        SESSION_NOT_FOUND,
        SOURCE_NOT_FOUND,
        RESULT_NOT_FOUND,
        ROUTE_NOT_FOUND,
        METHOD_NOT_ALLOWED,
        SESSION_BUSY,
        REQUEST_ID_CONFLICT,
        SESSION_METADATA_INVALID,
        SOURCE_INACTIVE,
        SOURCE_CHANGED,
        SOURCE_MISMATCH,
        SESSION_BINDING_MISSING,
        KNOWLEDGE_CHANGED,
        SESSION_INVALIDATED,
        SOURCE_NOT_READY,
        SOURCE_UNSUPPORTED,
        SOURCE_CONFIG_INVALID,
        ORIGIN_NOT_ALLOWED,
        HOST_NOT_ALLOWED,
        UNSUPPORTED_MEDIA_TYPE,
        QUERY_CAPACITY_EXCEEDED,
        RATE_LIMITED,
        LOCK_BACKEND_ERROR,
        SOURCE_REGISTRY_UNAVAILABLE,
        DEPENDENCY_UNAVAILABLE,
        HISTORY_UNAVAILABLE,
        HISTORY_RECORD_INVALID,
        RESULT_INTEGRITY_ERROR,
        RESULT_ENCODING_ERROR,
        INTERNAL_ERROR,
    }
)

# HTTP 200 的 WebQueryResult.error_code 闭集（含前置拒绝附 data.result 的码）
TERMINAL_ERROR_CODES = frozenset(
    {
        "partial",
        "needs_clarification",
        "no_data",
        "query_failed",
        "budget_exhausted",
        "persistence_error",
        RESULT_ENCODING_ERROR,
        KNOWLEDGE_CHANGED,
        DEPENDENCY_UNAVAILABLE,
    }
)

# readiness checks.error_code 闭集（§5.5）：非 200 error_code 加业务源检查码
READINESS_CHECK_CODES = frozenset(
    ERROR_CODES
    | {
        "business_target_mismatch",
        "business_readonly_invalid",
        "business_timeout_invalid",
        "readonly_default_off",
        "knowledge_missing",
        "knowledge_inconsistent",
        "optional_knowledge_empty",
    }
)

# 稳定 HTTP 状态（doc 2 §4.2 表；429 附 Retry-After，405 保留 Allow）
HTTP_STATUS = {
    INVALID_REQUEST: 422,
    SESSION_NOT_FOUND: 404,
    SOURCE_NOT_FOUND: 404,
    RESULT_NOT_FOUND: 404,
    ROUTE_NOT_FOUND: 404,
    METHOD_NOT_ALLOWED: 405,
    SESSION_BUSY: 409,
    REQUEST_ID_CONFLICT: 409,
    SESSION_METADATA_INVALID: 409,
    SOURCE_INACTIVE: 409,
    SOURCE_CHANGED: 409,
    SOURCE_MISMATCH: 409,
    SESSION_BINDING_MISSING: 409,
    KNOWLEDGE_CHANGED: 409,
    SESSION_INVALIDATED: 409,
    SOURCE_NOT_READY: 409,
    SOURCE_UNSUPPORTED: 422,
    SOURCE_CONFIG_INVALID: 422,
    ORIGIN_NOT_ALLOWED: 403,
    HOST_NOT_ALLOWED: 400,
    UNSUPPORTED_MEDIA_TYPE: 415,
    QUERY_CAPACITY_EXCEEDED: 429,
    RATE_LIMITED: 429,
    LOCK_BACKEND_ERROR: 503,
    SOURCE_REGISTRY_UNAVAILABLE: 503,
    DEPENDENCY_UNAVAILABLE: 503,
    HISTORY_UNAVAILABLE: 503,
    HISTORY_RECORD_INVALID: 500,
    RESULT_INTEGRITY_ERROR: 500,
    RESULT_ENCODING_ERROR: 500,
    INTERNAL_ERROR: 500,
}


class WebApiError(Exception):
    """带稳定 error_code 的业务拒绝（doc 2 §4.2）。

    data 至少包含 {error_code}；路由层统一补 request_id/run_id 等定位字段。
    extra 允许附 readiness / result 等结构（前置拒绝附 data.result 时其
    error_code 使用同一拒绝码）。
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        data: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ):
        if error_code not in ERROR_CODES:
            raise ValueError(f"非闭集 error_code: {error_code!r}")
        self.error_code = error_code
        self.message = message
        self.data = dict(data or {})
        self.headers = dict(headers or {})
        super().__init__(message)

    @property
    def status_code(self) -> int:
        return HTTP_STATUS[self.error_code]
