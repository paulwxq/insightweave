// 字段与 backend/api/schemas.py 对应，改动需同步；本阶段不引入类型代码生成。

export interface ConnectionFields {
  db_type: 'postgresql'
  host: string
  port: number
  db_name: string
  username: string
  password: string
  sslmode: string | null
}

export interface ConnectionCheckResponse {
  server_version: string
  schemas: string[]
}

export interface DataSourceCreateRequest extends ConnectionFields {
  display_name: string
  selected_schemas: string[]
  description: string | null
  confirm_duplicate: boolean
}

export interface DataSourceUpdateRequest {
  db_type: 'postgresql'
  host: string
  port: number
  db_name: string
  username: string
  password?: string
  sslmode: string | null
  display_name: string
  selected_schemas: string[]
  description: string | null
  confirm_duplicate: boolean
}

export interface DataSourceListItem {
  source_id: string
  display_name: string
  db_type: string
  host: string
  port: number
  db_name: string
  username: string
  sslmode: string | null
  selected_schemas: string[]
  description: string | null
  db_version: string | null
  created_at: string
  is_active: boolean
  last_collected_at?: string | null
  last_published_at?: string | null
}

export interface DataSourceListResponse {
  items: DataSourceListItem[]
}

export interface CollectionJobSource {
  source_id: string
  display_name: string
  host: string
  port: number
  db_name: string
  db_type_version: string
  description: string | null
  is_active: boolean
}

export interface CollectionJobItem {
  job_id: string
  executed_at: string
  steps: string[]
  // 同一作业全部步骤行的汇总状态：running 优先，其次 failed，否则 success（doc 20.1 §11.2）
  job_status: 'success' | 'failed' | 'running'
  run_by_user_id: string | null
  // 本作业 domain 行的 db_desc；缺行或值为 NULL 时为 null（doc 21.1 §7.2）
  domain_description: string | null
}

export interface CollectionJobListResponse {
  source: CollectionJobSource
  job_count: number
  items: CollectionJobItem[]
}

export interface SqlReadinessResponse {
  ready: boolean
  missing: string[]
  message: string
}

export interface PublishJobItem {
  publish_id: string
  job_id: string
  published_at: string
  profile_steps: string[]
  rag_steps: string[]
  created_by_user_id: string | null
}

export interface PublishJobListResponse {
  source: CollectionJobSource
  job_count: number
  items: PublishJobItem[]
}

export interface PublishDefinitionResponse {
  publish_id: string
  job_id: string
  steps: string[]
}

export interface CollectionHomeItem {
  job_id: string
  publish_id: null
  executed_at: string
  steps: string[]
  status: 'running' | 'success' | 'failed'
}

export interface CollectionHomeResponse {
  is_active: boolean
  counts: { running: number; success_recent: number; failed: number }
  items: CollectionHomeItem[]
}

export interface PublishHomeItem {
  job_id: string
  publish_id: string
  executed_at: string
  steps: string[]
  status: 'success' | 'failed'
}

export interface PublishHomeResponse {
  is_active: boolean
  counts: { current: number; success_recent: number; failed: number }
  items: PublishHomeItem[]
}

export interface AssetCatalogItem {
  publish_id: string
  source_id: string
  display_name: string
  db_type: string
  db_type_label: string
  server_version: string | null
  connection: string
  table_count: number
  object_count: number
  size_bytes: number
  collected_at: string
}

export interface AssetCatalogResponse {
  source_count: number
  object_count: number
  project_count: number
  inconsistent_count: number
  items: AssetCatalogItem[]
}

export type AssetProfileState = 'ready' | 'not_published' | 'failed' | 'inconsistent'

export interface AssetProfileSource {
  source_id: string
  display_name: string
  username: string
}

export interface AssetProfilePublish {
  publish_id: string
  status: string
  error_summary: string | null
  published_at: string
}

export interface AssetProfileSchema {
  name: string
  ordinary_table_count: number
  partitioned_table_count: number
  partition_child_count: number
  view_count: number
  materialized_view_count: number
  object_count: number
}

export interface AssetProfileTotals {
  ordinary_table_count: number
  partitioned_table_count: number
  partition_child_count: number
  view_count: number
  materialized_view_count: number
  object_count: number
}

export interface AssetProfileDetail {
  db_type: string
  db_type_label: string
  db_name: string
  host: string
  port: number
  server_version: string | null
  server_version_full: string | null
  os_platform: string | null
  collected_at: string
  size_bytes: number
  size_scope: string
  record_count: number | null
  selected_schemas: string[]
  schemas: AssetProfileSchema[]
  totals: AssetProfileTotals
}

export interface AssetProfileResponse {
  state: AssetProfileState
  source: AssetProfileSource
  publish: AssetProfilePublish | null
  profile: AssetProfileDetail | null
}

export interface CollectionJobStepResult {
  step: string
  status: 'success' | 'failed'
  message: string
  finished_at: string
}

export interface CollectionJobCreateResponse {
  job_id: string
  results: CollectionJobStepResult[]
}

export interface PublishCheckStep {
  step_name: 'db' | 'ddl' | 'json' | 'domain' | 'table_schema' | 'dim_value' | 'rel' | 'sql'
  job_status: 'running' | 'success' | 'failed' | null
  output_ready: boolean
  publishable: boolean
  reason: string | null
}

export interface PublishCheckResponse {
  source_id: string
  job_id: string
  steps: PublishCheckStep[]
}

export interface PublicationCreateRequest {
  steps: string[]
}

export interface PublicationStepResult {
  step: 'db' | 'ddl' | 'json' | 'domain' | 'table_schema' | 'dim_value' | 'rel' | 'sql'
  status: 'success' | 'failed'
  message: string
  finished_at: string
}

export interface PublicationCreateResponse {
  publish_id: string
  results: PublicationStepResult[]
}

export interface DuplicateSourceDetail {
  source_id: string
  display_name: string
}

export interface ApiErrorResponse {
  code: string
  message: string
  details?: DuplicateSourceDetail[]
}

// ---- 对象画像读取（doc 19.2 §8.1.1） ----

export type ObjectState = 'ready' | 'not_published' | 'inconsistent'
export type PrimaryKeyState = 'present' | 'none' | 'unknown'

export interface JsonPublishInfo {
  publish_id: string
  job_id: string
  published_at: string
}

export interface DdlPublishInfo {
  publish_id: string
  job_id: string
  loaded_at: string
}

export interface ObjectListItem {
  schema_name: string
  object_name: string
  object_type: string
  object_comment: string | null
  table_category: string | null
  column_count: number
  total_rows: number | null
  primary_key_state: PrimaryKeyState
  generated_timestamp: string | null
}

export interface ObjectsResponse {
  state: ObjectState
  source_id: string
  json_publish: JsonPublishInfo | null
  total: number
  page: number
  page_size: number
  items: ObjectListItem[]
}

export interface ObjectOptionItem {
  schema_name: string
  object_name: string
  object_type: string
}

export interface ObjectOptionsResponse {
  state: ObjectState
  source_id: string
  json_publish: JsonPublishInfo | null
  object_types: string[]
  schemas: string[]
  objects: ObjectOptionItem[]
}

export interface ColumnPhysicalConstraints {
  primary_key_state: PrimaryKeyState
  primary_key: Record<string, unknown> | null
  foreign_keys: Record<string, unknown>[] | null
  unique_constraints: Record<string, unknown>[] | null
}

export interface ObjectDetailColumn {
  // §8.1.1：字段行返回 §6 字段行标量（仅排除 value_distribution）
  source_id: string
  schema_name: string
  object_name: string
  publish_id: string
  column_name: string
  ordinal_position: number
  data_type: string
  is_nullable: boolean
  column_default: string | null
  column_comment: string | null
  comment_source: string | null
  semantic_role: string | null
  semantic_confidence: number | null
  semantic_basis: string[] | null
  null_count: number | null
  null_rate: number | null
  unique_count: number | null
  uniqueness: number | null
  sample_min: string | null
  sample_max: string | null
  physical_constraints: ColumnPhysicalConstraints
  indexes: Record<string, unknown>[] | null
  unique_column_sets: Record<string, unknown>[] | null
}

export interface ObjectDetailProfile {
  source_id: string
  schema_name: string
  object_name: string
  db_name: string
  object_type: string
  publish_id: string
  job_id: string
  metadata_version: string
  generated_timestamp: string | null
  loaded_at: string
  object_comment: string | null
  object_comment_source: string | null
  total_rows: number | null
  column_count: number
  sample_method: string | null
  sample_count: number | null
  has_table_profile: boolean
  table_category: string | null
  category_confidence: number | null
  classification_source: string | null
  classification_reason: string | null
  classification_basis: string[] | null
  rule_table_category: string | null
  rule_confidence: number | null
  rule_inference_basis: string[] | null
  primary_key_name: string | null
  primary_key_columns: string[] | null
  foreign_key_count: number | null
  index_count: number | null
  constraint_count: number | null
  foreign_keys: Record<string, unknown>[] | null
  unique_constraints: Record<string, unknown>[] | null
  indexes: Record<string, unknown>[] | null
  unique_column_sets: Record<string, unknown>[] | null
}

export interface ObjectDetailResponse {
  state: 'ready' | 'not_published'
  source_id: string
  schema_name: string
  object_name: string
  json_publish: JsonPublishInfo | null
  ddl_state: 'ready' | 'not_published' | 'incompatible' | 'inconsistent'
  ddl_publish: DdlPublishInfo | null
  profile: ObjectDetailProfile | null
  columns: ObjectDetailColumn[]
}

export interface ObjectDdlResponse {
  state: 'ready' | 'not_published' | 'incompatible'
  json_publish_id: string | null
  ddl_publish: DdlPublishInfo | null
  definition_sql: string | null
  sample_state: 'ready' | 'no_sample_block' | 'empty' | null
  sample_columns: string[]
  sample_records: Record<string, unknown>[]
}

export interface DomainConfigDomain {
  name: string
  description: string
  tables: string[]
}

export interface DomainConfigResponse {
  fingerprint: string
  database: { name: string; description: string | null }
  max_domains_per_table: number | null
  domains: DomainConfigDomain[]
}

export interface DomainConfigUpdateRequest {
  fingerprint: string
  database: { name: string; description: string | null }
  max_domains_per_table: number | null
  domains: DomainConfigDomain[]
}

export interface DomainConfigTableOption {
  id: string
  label: string
}

export interface DomainConfigTablesResponse {
  tables: DomainConfigTableOption[]
}

export interface DimValueConfigTable {
  schema_name: string
  object_name: string
  embedding_col: string | null
}

export interface DimValueConfigGetResponse {
  tables: DimValueConfigTable[]
  version: string
}

export interface DimValueConfigUpdateRequest {
  tables: DimValueConfigTable[]
  version: string
}

export interface DimValueObject {
  schema_name: string
  object_name: string
  object_type: string
}

export interface DimValueObjectsResponse {
  objects: DimValueObject[]
}

export interface DimValueColumn {
  name: string
  data_type: string
  comment: string
  sample_value: string
}

export interface DimValueColumnsResponse {
  columns: DimValueColumn[]
}

// ---- rel 关系编辑（doc 23.2 §4）----

export interface RelConfigTableRef {
  schema: string
  table: string
}

export interface RelConfigRelationshipInput {
  relationship_id?: string
  from_table: RelConfigTableRef
  to_table: RelConfigTableRef
  from_columns: string[]
  to_columns: string[]
  cardinality: string
}

export type RelConfigRelationship = Record<string, unknown> & {
  relationship_id: string
  from_table: RelConfigTableRef
  to_table: RelConfigTableRef
}

export interface RelConfigGetResponse {
  database: string
  fingerprint: string
  tables: string[]
  relationships: RelConfigRelationship[]
  /** cql_generation.composite_score_threshold，与 CQL 生成同一阈值 */
  score_threshold: number
}

export interface RelConfigColumnsResponse {
  columns: string[]
}

export interface RelConfigUpdateRequest {
  fingerprint: string
  relationships: RelConfigRelationshipInput[]
}

export interface RelConfigUpdateResponse {
  fingerprint: string
  relationships: RelConfigRelationship[]
}
