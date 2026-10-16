"""对象读取接口响应模型（doc 19.2 §8.1.1）。"""

from pydantic import BaseModel


class JsonPublishInfo(BaseModel):
    publish_id: str
    job_id: str
    published_at: str


class DdlPublishInfo(BaseModel):
    publish_id: str
    job_id: str
    loaded_at: str


class ObjectListItem(BaseModel):
    schema_name: str
    object_name: str
    object_type: str
    object_comment: str | None
    table_category: str | None
    column_count: int
    total_rows: int | None
    primary_key_state: str
    generated_timestamp: str | None


class ObjectsResponse(BaseModel):
    state: str
    source_id: str
    json_publish: JsonPublishInfo | None
    total: int
    page: int
    page_size: int
    items: list[ObjectListItem]


class ObjectOptionItem(BaseModel):
    schema_name: str
    object_name: str
    object_type: str


class ObjectOptionsResponse(BaseModel):
    state: str
    source_id: str
    json_publish: JsonPublishInfo | None
    object_types: list[str]
    schemas: list[str]
    objects: list[ObjectOptionItem]


class ColumnPhysicalConstraints(BaseModel):
    primary_key_state: str
    primary_key: dict | None
    foreign_keys: list[dict] | None
    unique_constraints: list[dict] | None


class ObjectDetailColumn(BaseModel):
    # §6 字段行标量（仅排除 value_distribution，§8.1.1）
    source_id: str
    schema_name: str
    object_name: str
    publish_id: str
    column_name: str
    ordinal_position: int
    data_type: str
    is_nullable: bool
    column_default: str | None
    column_comment: str | None
    comment_source: str | None
    semantic_role: str | None
    semantic_confidence: float | None
    semantic_basis: list[str] | None
    null_count: int | None
    null_rate: float | None
    unique_count: int | None
    uniqueness: float | None
    sample_min: str | None
    sample_max: str | None
    physical_constraints: ColumnPhysicalConstraints
    indexes: list[dict] | None
    unique_column_sets: list[dict] | None


class ObjectDetailProfile(BaseModel):
    """§6 表级标量全量（除 json_sample_records），字段名与库表列一致。"""

    source_id: str
    schema_name: str
    object_name: str
    db_name: str
    object_type: str
    publish_id: str
    job_id: str
    metadata_version: str
    generated_timestamp: str | None
    loaded_at: str
    object_comment: str | None
    object_comment_source: str | None
    total_rows: int | None
    column_count: int
    sample_method: str | None
    sample_count: int | None
    has_table_profile: bool
    table_category: str | None
    category_confidence: float | None
    classification_source: str | None
    classification_reason: str | None
    classification_basis: list[str] | None
    rule_table_category: str | None
    rule_confidence: float | None
    rule_inference_basis: list[str] | None
    primary_key_name: str | None
    primary_key_columns: list[str] | None
    foreign_key_count: int | None
    index_count: int | None
    constraint_count: int | None
    foreign_keys: list[dict] | None
    unique_constraints: list[dict] | None
    indexes: list[dict] | None
    unique_column_sets: list[dict] | None


class ObjectDetailResponse(BaseModel):
    state: str
    source_id: str
    schema_name: str
    object_name: str
    json_publish: JsonPublishInfo | None
    ddl_state: str
    ddl_publish: DdlPublishInfo | None
    profile: ObjectDetailProfile | None
    columns: list[ObjectDetailColumn]


class ObjectDdlResponse(BaseModel):
    state: str
    json_publish_id: str | None
    ddl_publish: DdlPublishInfo | None
    definition_sql: str | None
    sample_state: str | None
    sample_columns: list[str]
    sample_records: list[dict]
