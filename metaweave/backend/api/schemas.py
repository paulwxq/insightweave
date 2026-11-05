"""API 请求/响应模型（doc 6.2 §6.1；source_id 为短 ID）。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool


# ---- 数据源登记（保留既有能力，参数改短 ID） ----


class ConnectionCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    db_type: Literal["postgresql"] = "postgresql"
    host: str
    port: int = Field(ge=1, le=65535)
    db_name: str
    username: str
    password: str = Field(repr=False, min_length=1)
    sslmode: str | None = None


class ConnectionCheckResponse(BaseModel):
    """连接检查成功载荷（doc 11）：200 即成功，无 ok 字段。"""

    server_version: str | None = None
    schemas: list[str] = []


class DataSourceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    db_type: Literal["postgresql"] = "postgresql"
    host: str
    port: int = Field(ge=1, le=65535)
    db_name: str
    username: str
    password: str = Field(repr=False, min_length=1)
    sslmode: str | None = None
    display_name: str
    selected_schemas: list[str]
    description: str | None = None
    confirm_duplicate: bool = False


class DataSourceUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    db_type: Literal["postgresql"] = "postgresql"
    host: str
    port: int = Field(ge=1, le=65535)
    db_name: str
    username: str
    sslmode: str | None = None
    password: str | None = Field(default=None, repr=False)
    display_name: str
    selected_schemas: list[str]
    description: str | None = None
    confirm_duplicate: bool = False


class DataSourceActivationRequest(BaseModel):
    """`is_active` 用 StrictBool：只接受 JSON true/false，字符串与数字一律 422。"""

    model_config = ConfigDict(extra="forbid")

    is_active: StrictBool


class DeletionConfirmRequest(BaseModel):
    """删除/清理确认体：必须回传目标 ID（doc 8.1 §4/§7）。"""

    confirm_id: str


class DeletionTargetConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    previous_fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")
    current_fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")


class DeletionRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_confirmation: DeletionTargetConfirmation | None = None


class DeletionTaskItem(BaseModel):
    task_id: str
    action: str
    status: str
    source_id: str
    workspace_id: str | None = None
    target_id: str
    error_message: str | None = None
    created_at: str | None = None
    queued_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class DeletionTaskDetail(DeletionTaskItem):
    can_retry: bool
    retry_reason: str | None = None
    steps: dict[str, Any] = {}


class DeletionTaskSummary(BaseModel):
    """对象响应附带的删除任务摘要（doc 8.1 §7.0）。"""

    task_id: str
    action: str
    status: str
    source_id: str
    workspace_id: str | None = None
    target_id: str
    error_message: str | None = None


class HistorySourceSummary(BaseModel):
    """历史来源摘要（doc 8.1 §8.2）：不含连接字段，不解密、不建连。"""

    source_id: str
    display_name: str
    deleted: bool


class DataSourceListItem(BaseModel):
    source_id: str
    display_name: str
    db_type: str
    host: str
    port: int
    db_name: str
    username: str
    sslmode: str | None = None
    selected_schemas: list[str]
    description: str | None = None
    db_version: str | None = None
    created_at: str
    is_active: bool
    last_collected_at: str | None = None
    last_published_at: str | None = None
    deletion_task: DeletionTaskSummary | None = None


class DataSourceListResponse(BaseModel):
    items: list[DataSourceListItem]


# ---- 工作区 ----


class WorkspaceCreateRequest(BaseModel):
    workspace_name: str
    workspace_desc: str | None = None


class WorkspaceItem(BaseModel):
    workspace_id: str
    source_id: str
    workspace_name: str
    workspace_desc: str | None = None
    created_at: str
    updated_at: str
    deletion_task: DeletionTaskSummary | None = None
    effective_state: str | None = None
    blocked_by: dict[str, str] | None = None
    blocked_reason: str | None = None


class WorkspacePageItem(WorkspaceItem):
    """跨源列表行（doc 6.2 §6.1）：附每工作区采集/发布作业数量。"""

    collection_job_count: int
    publish_job_count: int


class WorkspaceListResponse(BaseModel):
    source_id: str
    items: list[WorkspaceItem]


class WorkspacePageResponse(BaseModel):
    """跨源工作区列表分页信封（doc 6.2 §6.1）"""

    items: list[WorkspacePageItem]
    total: int
    limit: int
    offset: int


class WorkspacePatchRequest(BaseModel):
    workspace_name: str | None = None
    workspace_desc: str | None = None


# ---- 作业与版本 ----


class JobConfigPayload(BaseModel):
    steps: list[str]
    step_config: dict[str, dict[str, Any]] | None = None
    change_note: str | None = None


class JobCreateRequest(BaseModel):
    job_name: str
    job_desc: str | None = None
    config: JobConfigPayload


class JobPatchRequest(BaseModel):
    job_name: str | None = None
    job_desc: str | None = None


class RevisionItem(BaseModel):
    job_id: str
    revision_no: int
    steps: list[str]
    step_config: dict[str, Any] | None = None
    change_note: str | None = None
    created_at: str


class JobItem(BaseModel):
    job_id: str
    workspace_id: str
    job_name: str
    job_desc: str | None = None
    created_at: str
    updated_at: str
    job_type: str
    latest_revision_no: int | None = None
    revision: RevisionItem | None = None
    default_steps: list[str] = []
    run_count: int = 0


class JobListResponse(BaseModel):
    workspace_id: str
    job_type: str
    items: list[JobItem]


class GlobalJobItem(JobItem):
    """全局作业列表行（设计稿 35/37）：在工作区作业之上附源/工作区名与最近运行。"""

    workspace_name: str = ""
    source_id: str = ""
    source_name: str = ""
    last_run_at: str | None = None


class GlobalJobListResponse(BaseModel):
    job_type: str
    workspace_id: str | None = None
    source_id: str | None = None
    q: str | None = None
    total: int
    limit: int
    offset: int
    items: list[GlobalJobItem]


class RevisionListResponse(BaseModel):
    job_id: str
    items: list[RevisionItem]


# ---- 运行 ----


class RunCreateRequest(BaseModel):
    revision_no: int
    mode: str = "default"  # default | custom
    steps: list[str] | None = None
    step_config: dict[str, dict[str, Any]] | None = None


class RunStepItem(BaseModel):
    step_name: str
    status: str
    message: str | None = None
    step_config: dict[str, Any] | None = None
    input_manifest: dict[str, Any] | None = None
    output_manifest: dict[str, Any] | None = None
    target_manifest: dict[str, Any] | None = None
    started_at: str | None = None
    finished_at: str | None = None


class CollectionRunDetail(BaseModel):
    # 发布运行详情（PublishRunDetail）只有 publish_run_id，无采集运行 ID，
    # 故此字段可选：缺失时由采集/发布各自的路由语义保证正确性
    collection_run_id: str | None = None
    job_id: str
    job_name: str | None = None
    workspace_id: str | None = None
    revision_no: int
    trigger_type: str
    status: str
    message: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    run_type: str
    steps: list[RunStepItem]


class PublishRunDetail(CollectionRunDetail):
    publish_run_id: str


class RunListItem(BaseModel):
    run_id: str
    job_id: str
    job_name: str | None = None
    revision_no: int
    status: str
    trigger_type: str
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    run_type: str


class RunListResponse(BaseModel):
    workspace_id: str | None = None
    job_id: str | None = None
    run_type: str
    total: int
    limit: int
    offset: int
    items: list[RunListItem]


# ---- 发布预检 ----


class PublishCheckStep(BaseModel):
    step_name: str
    ready: bool
    reason: str | None = None


class PublishCheckDirectory(BaseModel):
    directory: str
    marker_status: str
    collection_run_id: str | None = None
    collected_at: str | None = None
    error: str | None = None


class PublishCheckResponse(BaseModel):
    workspace_id: str
    source_id: str
    steps: list[PublishCheckStep]
    input_directories: list[PublishCheckDirectory]


class PublishJobPrecheckRequest(BaseModel):
    """作业级预检请求（doc 7.2 §5.2）：与创建运行同一解析规则，不创建运行。"""

    revision_no: int
    mode: str = "default"  # default | custom
    steps: list[str] | None = None
    step_config: dict[str, dict[str, Any]] | None = None


class PublishJobPrecheckStep(BaseModel):
    step_name: str
    ready: bool
    reason: str | None = None
    code: str | None = None


class PublishJobPrecheckResponse(BaseModel):
    publish_job_id: str
    revision_no: int
    mode: str
    steps: list[PublishJobPrecheckStep]


class DimValueRecommendationsResponse(BaseModel):
    """独立推荐读取（doc 7.2 §4）：与被编辑 YAML 解耦的只读推荐。"""

    tables: list[dict[str, Any]]


# ---- 配置编辑（工作区定位） ----


class DomainConfigResponse(BaseModel):
    fingerprint: str
    database: dict[str, Any]
    max_domains_per_table: int | None = None
    domains: list[dict[str, Any]]


class DomainConfigTablesResponse(BaseModel):
    tables: list[dict[str, Any]]


class DomainConfigUpdateRequest(BaseModel):
    fingerprint: str
    database: dict[str, Any]
    domains: list[dict[str, Any]]


class RelConfigGetResponse(BaseModel):
    database: str
    fingerprint: str
    tables: list[str]
    relationships: list[dict[str, Any]]
    score_threshold: float


class RelConfigColumnsResponse(BaseModel):
    columns: list[str]


class RelConfigUpdateRequest(BaseModel):
    fingerprint: str
    relationships: list[dict[str, Any]]


class RelConfigUpdateResponse(BaseModel):
    fingerprint: str
    relationships: list[dict[str, Any]]


# ---- 数据资产 ----


class AssetCatalogItem(BaseModel):
    publish_run_id: str
    source_id: str
    display_name: str
    db_type: str
    db_type_label: str
    server_version: str | None
    connection: str
    table_count: int
    object_count: int
    size_bytes: int
    collected_at: str
    published_at: str | None = None


class AssetCatalogResponse(BaseModel):
    source_count: int
    object_count: int
    project_count: int
    items: list[AssetCatalogItem]


class AssetProfileSource(BaseModel):
    source_id: str
    display_name: str
    username: str


class AssetProfilePublish(BaseModel):
    publish_run_id: str
    status: str
    published_at: str


class AssetProfileDetail(BaseModel):
    db_type: str
    db_type_label: str
    db_name: str
    host: str
    port: int
    server_version: str | None
    server_version_full: str | None
    os_platform: str | None
    collected_at: str
    size_bytes: int
    size_scope: str
    record_count: int | None
    selected_schemas: list[str]
    schemas: list[dict]
    totals: dict


class AssetProfileResponse(BaseModel):
    state: str
    source: AssetProfileSource
    publish: AssetProfilePublish | None
    profile: AssetProfileDetail | None


class DimValueConfigGetResponse(BaseModel):
    tables: list[dict[str, Any]]
    version: str
    """embedding_col 为含逗号字符串的表（读取按未指定处理；供显式初始化页
    提示改用字段列表格式，doc 7.2 §3.1.1/验收 25）"""
    comma_string_tables: list[str] = []


class DimValueObjectsResponse(BaseModel):
    objects: list[dict[str, Any]]


class DimValueColumnsResponse(BaseModel):
    columns: list[dict[str, Any]]


class DimValueConfigUpdateRequest(BaseModel):
    version: str
    tables: list[dict[str, Any]]


class DimValueConfigUpdateResponse(BaseModel):
    tables: list[dict[str, Any]]
