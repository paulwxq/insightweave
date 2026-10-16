"""数据源登记 API 的请求和安全响应模型。"""

from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from metaweave.core.metadata.db_step import validate_schema_names
from metaweave.db.postgres_options import normalize_sslmode


class ConnectionFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    db_type: Literal["postgresql"]
    host: str
    port: int = Field(ge=1, le=65535)
    db_name: str
    username: str
    password: str = Field(repr=False, min_length=1)
    sslmode: str | None = None

    @field_validator("host", "db_name", "username")
    @classmethod
    def nonblank_connection_field(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("连接字段不能为空")
        return value

    @field_validator("sslmode")
    @classmethod
    def normalize_sslmode_field(cls, value: str | None) -> str | None:
        """doc 18.1 §4.1：非法模式 422 invalid_request；合法值规范化后保存。"""
        return normalize_sslmode(value)


class ConnectionCheckRequest(ConnectionFields):
    pass


class DataSourceCreateRequest(ConnectionFields):
    display_name: str
    selected_schemas: list[str]
    description: str | None = None
    confirm_duplicate: bool = False

    @field_validator("display_name")
    @classmethod
    def nonblank_display_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("数据源显示名称不能为空")
        return value

    @field_validator("selected_schemas")
    @classmethod
    def valid_schema_names(cls, value: list[str]) -> list[str]:
        validate_schema_names(value)
        return value

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if len(value) > 1000:
            raise ValueError("数据源说明不能超过 1000 个字符")
        return value or None


class ConnectionCheckResponse(BaseModel):
    server_version: str
    schemas: list[str]


class DataSourceActivationRequest(BaseModel):
    """启停数据源（doc 11 §5）。

    `is_active` 用 `StrictBool`：只接受 JSON 的 true/false，字符串与数字一律 422，
    不让"传错类型"被宽松转换悄悄吞掉。
    """

    model_config = ConfigDict(extra="forbid")

    is_active: StrictBool


class DataSourceUpdateRequest(BaseModel):
    """整份替换。sslmode 与 description 必填且可为 null，不继承创建模型的默认值。"""

    model_config = ConfigDict(extra="forbid")

    db_type: Literal["postgresql"]
    host: str
    port: int = Field(ge=1, le=65535)
    db_name: str
    username: str
    password: str | None = Field(default=None, repr=False)
    sslmode: str | None
    display_name: str
    selected_schemas: list[str]
    description: str | None
    confirm_duplicate: bool = False

    @field_validator("host", "db_name", "username")
    @classmethod
    def nonblank_connection_field(cls, value: str) -> str:
        return ConnectionFields.nonblank_connection_field(value)

    @field_validator("sslmode")
    @classmethod
    def normalize_sslmode_field(cls, value: str | None) -> str | None:
        return ConnectionFields.normalize_sslmode_field(value)

    @field_validator("password")
    @classmethod
    def empty_password_means_unchanged(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return value

    @field_validator("display_name")
    @classmethod
    def nonblank_display_name(cls, value: str) -> str:
        return DataSourceCreateRequest.nonblank_display_name(value)

    @field_validator("selected_schemas")
    @classmethod
    def valid_schema_names(cls, value: list[str]) -> list[str]:
        return DataSourceCreateRequest.valid_schema_names(value)

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        return DataSourceCreateRequest.normalize_description(value)


class DataSourceListItem(BaseModel):
    source_id: str
    display_name: str
    db_type: str
    host: str
    port: int
    db_name: str
    username: str
    sslmode: str | None
    selected_schemas: list[str]
    description: str | None
    db_version: str | None
    created_at: str
    is_active: bool


class DataSourceCatalogItem(DataSourceListItem):
    """列表卡片额外带出的最近采集与最近发布；单条读写接口不返回这两项。"""

    last_collected_at: str | None
    last_published_at: str | None


class DataSourceListResponse(BaseModel):
    items: list[DataSourceCatalogItem]


class DuplicateSourceDetail(BaseModel):
    source_id: str
    display_name: str


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: list[DuplicateSourceDetail] | None = None


class CollectionJobSource(BaseModel):
    source_id: str
    display_name: str
    host: str
    port: int
    db_name: str
    db_type_version: str
    description: str | None
    is_active: bool


class CollectionJobItem(BaseModel):
    job_id: str
    executed_at: str
    steps: list[str]
    job_status: Literal["success", "failed", "running"]
    run_by_user_id: str | None


class CollectionJobListResponse(BaseModel):
    source: CollectionJobSource
    job_count: int
    items: list[CollectionJobItem]


class PublishJobItem(BaseModel):
    publish_id: str
    job_id: str
    published_at: str
    profile_steps: list[str]
    rag_steps: list[str]
    created_by_user_id: str | None


class PublishJobListResponse(BaseModel):
    source: CollectionJobSource
    job_count: int
    items: list[PublishJobItem]


class CollectionHomeItem(BaseModel):
    job_id: str
    publish_id: None = None
    executed_at: str
    steps: list[str]
    status: Literal["running", "success", "failed"]


class CollectionHomeCounts(BaseModel):
    running: int
    success_recent: int
    failed: int


class CollectionHomeResponse(BaseModel):
    is_active: bool
    counts: CollectionHomeCounts
    items: list[CollectionHomeItem]


class PublishHomeItem(BaseModel):
    job_id: str
    publish_id: str
    executed_at: str
    steps: list[str]
    status: Literal["success", "failed"]


class PublishHomeCounts(BaseModel):
    current: int
    success_recent: int
    failed: int


class PublishHomeResponse(BaseModel):
    is_active: bool
    counts: PublishHomeCounts
    items: list[PublishHomeItem]


class PublishDefinitionResponse(BaseModel):
    publish_id: str
    job_id: str
    steps: list[str]


class AssetCatalogItem(BaseModel):
    publish_id: str
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


class AssetCatalogResponse(BaseModel):
    source_count: int
    object_count: int
    project_count: int
    inconsistent_count: int
    items: list[AssetCatalogItem]


class AssetProfileSource(BaseModel):
    source_id: str
    display_name: str
    username: str


class AssetProfilePublish(BaseModel):
    publish_id: str
    status: str
    error_summary: str | None
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
    state: Literal["ready", "not_published", "failed", "inconsistent"]
    source: AssetProfileSource
    publish: AssetProfilePublish | None
    profile: AssetProfileDetail | None


class CollectionJobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[str]
    job_id: UUID | None = None
    domain_description: str | None = None
    sync_domain_description: StrictBool = False

    @field_validator("domain_description", mode="before")
    @classmethod
    def normalize_domain_description(cls, value: str | None) -> str:
        """只在字段被提供时执行（doc 21.1 §7.1）：省略时不运行、默认 None 保留。

        显式 null、非字符串、strip() 后为空、超过 1000 字符一律 ValueError
        （FastAPI 422）；通过后存 strip() 规范值。
        """
        if value is None:
            raise ValueError("domain_description 不允许显式 null")
        if not isinstance(value, str):
            raise ValueError("domain_description 必须是字符串")
        text = value.strip()
        if not text:
            raise ValueError("domain_description 不能为空")
        if len(text) > 1000:
            raise ValueError("domain_description 不能超过 1000 个字符")
        return text

    @model_validator(mode="after")
    def validate_sync_pairing(self) -> "CollectionJobCreateRequest":
        """sync=true 必须与显式合法描述、本次 domain 步骤成对（doc 21.1 §7.1）。"""
        if self.sync_domain_description:
            if (
                "domain_description" not in self.model_fields_set
                or not self.domain_description
            ):
                raise ValueError(
                    "sync_domain_description=true 必须同时携带 domain_description"
                )
            if "domain" not in self.steps:
                raise ValueError(
                    "sync_domain_description=true 要求本次 steps 包含 domain"
                )
        return self


class CollectionJobStepResult(BaseModel):
    step: str
    status: Literal["success", "failed"]
    message: str
    finished_at: str


class CollectionJobCreateResponse(BaseModel):
    job_id: str
    results: list[CollectionJobStepResult]


class SqlReadinessResponse(BaseModel):
    ready: bool
    missing: list[str]
    message: str


class PublishCheckStep(BaseModel):
    step_name: Literal[
        "db", "ddl", "json", "domain", "table_schema", "dim_value", "rel", "sql"
    ]
    job_status: Literal["running", "success", "failed"] | None
    output_ready: bool
    publishable: bool
    reason: str | None


class PublishCheckResponse(BaseModel):
    source_id: str
    job_id: str
    steps: list[PublishCheckStep]


class DimValueConfigTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_name: str
    object_name: str
    embedding_col: str | None = None


class DimValueConfigGetResponse(BaseModel):
    tables: list[DimValueConfigTable]
    version: str


class DimValueConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[DimValueConfigTable]
    version: str


class DimValueConfigUpdateResponse(BaseModel):
    """PUT 成功响应只含 tables（doc 22.2 §5.2 第 5 步：version 不变，前端沿用 GET 值）。"""

    tables: list[DimValueConfigTable]


class DimValueObject(BaseModel):
    schema_name: str
    object_name: str
    object_type: str


class DimValueObjectsResponse(BaseModel):
    objects: list[DimValueObject]


class DimValueColumn(BaseModel):
    name: str
    data_type: str
    comment: str
    sample_value: str = ""


class DimValueColumnsResponse(BaseModel):
    columns: list[DimValueColumn]


class RelConfigRelationship(BaseModel):
    """PUT 客户端条目：实质字段是唯一输入（doc 23.2 §4.2）。

    表引用为宽松 dict（形态校验在服务层）；多余字段（分数/明细/方法等
    非实质字段）默认被忽略，篡改到不了服务层。
    """

    relationship_id: str | None = None
    from_table: dict
    to_table: dict
    from_columns: list[str]
    to_columns: list[str]
    cardinality: str


class RelConfigGetResponse(BaseModel):
    database: str
    fingerprint: str
    tables: list[str]
    relationships: list[dict]
    # cql_generation.composite_score_threshold；弹窗只展示达到该值的非外键关系
    score_threshold: float


class RelConfigColumnsResponse(BaseModel):
    columns: list[str]


class RelConfigUpdateRequest(BaseModel):
    fingerprint: str
    relationships: list[RelConfigRelationship]


class RelConfigUpdateResponse(BaseModel):
    """PUT 成功响应回带新指纹（doc 23.2 §4：前端保存后刷新指纹的数据来源）。"""

    fingerprint: str
    relationships: list[dict]


class DomainConfigDatabase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None


class DomainConfigDomain(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    tables: list[str]


class DomainConfigResponse(BaseModel):
    fingerprint: str
    database: DomainConfigDatabase
    max_domains_per_table: int | None
    domains: list[DomainConfigDomain]


class DomainConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fingerprint: str
    database: DomainConfigDatabase
    max_domains_per_table: int | None = None
    domains: list[DomainConfigDomain]


class DomainConfigTableOption(BaseModel):
    id: str
    label: str


class DomainConfigTablesResponse(BaseModel):
    tables: list[DomainConfigTableOption]


class PublicationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[str]


class PublicationStepResult(BaseModel):
    step: Literal[
        "db", "ddl", "json", "domain", "table_schema", "dim_value", "rel", "sql"
    ]
    status: Literal["success", "failed"]
    message: str
    finished_at: str


class PublicationCreateResponse(BaseModel):
    publish_id: str
    results: list[PublicationStepResult]
