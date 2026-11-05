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
  server_version: string | null
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
  /** doc 8.1 §7.0：未解决删除/清理任务摘要；无任务为 null */
  deletion_task?: DeletionTaskSummary | null
}

// ---- 删除与知识清理（doc 8.1） ----

export interface DeletionTaskSummary {
  task_id: string
  action: 'delete_source' | 'delete_workspace' | 'clear_source_knowledge'
  status: 'pending' | 'running' | 'failed' | 'success'
  source_id: string
  workspace_id: string | null
  target_id: string
  error_message: string | null
}

export interface DeletionTaskItem extends DeletionTaskSummary {
  created_at: string | null
  queued_at: string | null
  started_at: string | null
  finished_at: string | null
}

export interface DeletionTaskDetail extends DeletionTaskItem {
  can_retry: boolean
  retry_reason: string | null
  steps: Record<string, unknown>
}

export interface DeletionTargets {
  fingerprint: string
  milvus: { host: string; port: string; database: string; collections: string[] }
  neo4j: { uri: string; database: string }
}

export interface DeletionTargetChange {
  previous: DeletionTargets
  current: DeletionTargets
}

export interface DeletionTargetConfirmation {
  previous_fingerprint: string
  current_fingerprint: string
}

export interface DeletionRunRef {
  type: string
  run_id: string
  status: string
  job_id: string
  job_name: string
  workspace_id: string
  created_at: string | null
  started_at: string | null
}

export interface SourceDeletionPreview {
  source_id: string
  workspace_count: number
  collection_job_count: number
  publish_job_count: number
  blocking_runs: DeletionRunRef[]
  targets: {
    milvus?: { host: string; port: string; database: string; collections: string[] }
    neo4j?: { uri: string; database: string }
    error?: string
  }
}

export interface WorkspaceDeletionPreview {
  workspace_id: string
  source_id: string
  collection_job_count: number
  publish_job_count: number
  blocking_runs: DeletionRunRef[]
}

export interface DataSourceListResponse {
  items: DataSourceListItem[]
}

// ---- 工作区（doc 6.2 §6.1） ----

export interface WorkspaceCreateRequest {
  workspace_name: string
  workspace_desc?: string | null
}

export interface WorkspacePatchRequest {
  workspace_name?: string | null
  workspace_desc?: string | null
}

export interface WorkspaceItem {
  workspace_id: string
  source_id: string
  workspace_name: string
  workspace_desc: string | null
  created_at: string
  updated_at: string
  /** doc 8.1 §7.0/§3.3.1：删除任务摘要与父级有效状态 */
  deletion_task?: DeletionTaskSummary | null
  effective_state?: string | null
  blocked_by?: { parent_type: string; source_id?: string; workspace_id?: string } | null
  blocked_reason?: string | null
}

/** 跨源列表行：WorkspaceItem 基础上附每工作区采集/发布作业数量（doc 6.2 §6.1） */
export interface WorkspacePageItem extends WorkspaceItem {
  collection_job_count: number
  publish_job_count: number
}

/** 源内工作区列表（旧形状，数据源详情等场景使用） */
export interface WorkspaceListResponse {
  source_id: string
  items: WorkspaceItem[]
}

/** 跨源工作区列表（doc 6.2 §6.1：服务端分页信封，source_id 可选过滤） */
export interface WorkspacePageResponse {
  items: WorkspacePageItem[]
  total: number
  limit: number
  offset: number
}

// ---- 作业与版本 ----

export type JobKind = 'collection' | 'publish'

export interface JobConfigPayload {
  steps: string[]
  step_config?: Record<string, Record<string, unknown>> | null
  change_note?: string | null
}

export interface JobCreateRequest {
  job_name: string
  job_desc?: string | null
  config: JobConfigPayload
}

export interface JobPatchRequest {
  job_name?: string | null
  job_desc?: string | null
}

export interface RevisionItem {
  job_id: string
  revision_no: number
  steps: string[]
  step_config: Record<string, unknown> | null
  change_note: string | null
  created_at: string
}

export interface JobItem {
  job_id: string
  workspace_id: string
  job_name: string
  job_desc: string | null
  created_at: string
  updated_at: string
  job_type: JobKind
  latest_revision_no: number | null
  /** 详情接口附带最新版本；列表接口不含 */
  revision?: RevisionItem | null
  /** 最新版本的默认步骤（列表/详情/创建响应均返回） */
  default_steps: string[]
  /** 该作业累计运行数（创建响应恒为 0） */
  run_count: number
}

export interface JobListResponse {
  workspace_id: string
  job_type: JobKind
  items: JobItem[]
}

/** 全局作业列表行（设计稿 35/37）：工作区作业之上附源/工作区名与最近运行 */
export interface GlobalJobItem extends JobItem {
  workspace_name: string
  source_id: string
  source_name: string
  /** 最近一次运行的创建时间；从未运行为 null */
  last_run_at: string | null
}

/** GET /api/collection-jobs、/api/publish-jobs 的分页信封 */
export interface GlobalJobListResponse {
  job_type: JobKind
  workspace_id: string | null
  source_id: string | null
  q: string | null
  total: number
  limit: number
  offset: number
  items: GlobalJobItem[]
}

export interface RevisionListResponse {
  job_id: string
  items: RevisionItem[]
}

// ---- 运行 ----

export type RunMode = 'default' | 'custom'

export interface RunCreateRequest {
  revision_no: number
  mode: RunMode
  steps?: string[] | null
  step_config?: Record<string, Record<string, unknown>> | null
}

/** POST /collection-jobs/{id}/runs 的 202 载荷 */
export interface CollectionRunCreated {
  collection_run_id: string
  collection_job_id: string
  revision_no: number
  steps: string[]
  status: string
}

/** POST /publish-jobs/{id}/runs 的 202 载荷 */
export interface PublishRunCreated {
  publish_run_id: string
  publish_job_id: string
  revision_no: number
  steps: string[]
  status: string
}

export type RunStatus = 'pending' | 'running' | 'success' | 'partial_success' | 'failed'
export type RunStepStatus = 'pending' | 'running' | 'success' | 'failed' | 'blocked'

export interface RunStepItem {
  step_name: string
  status: RunStepStatus
  message: string | null
  step_config: Record<string, unknown> | null
  input_manifest: Record<string, unknown> | null
  /** 采集运行独有 */
  output_manifest?: Record<string, unknown> | null
  /** 发布运行独有 */
  target_manifest?: Record<string, unknown> | null
  started_at: string | null
  finished_at: string | null
}

/** input_manifest.files 条目（doc 6.1 §11.1）：path 为工作区相对路径 */
export interface ManifestFileEntry {
  path: string
  collection_run_id: string | null
}

/** target_manifest.targets 条目（doc 6.1 §11.3）：按 type 携带目标键 */
export interface ManifestTargetEntry {
  type: 'postgresql' | 'milvus' | 'neo4j'
  database?: string
  schema?: string
  table?: string
  collection?: string
}

export interface RunDetail {
  collection_run_id?: string
  publish_run_id?: string
  job_id: string
  job_name: string | null
  workspace_id: string | null
  revision_no: number
  trigger_type: string
  status: RunStatus
  message: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  run_type: JobKind
  steps: RunStepItem[]
}

/**
 * 运行列表项。列表接口返回 run_id；runIdOf() 同时兼容
 * collection_run_id / publish_run_id。
 */
export interface RunListItem {
  run_id?: string
  collection_run_id?: string
  publish_run_id?: string
  job_id: string
  job_name: string | null
  revision_no: number
  status: RunStatus
  trigger_type: string
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  run_type: JobKind
}

export interface RunListResponse {
  workspace_id?: string | null
  job_id?: string | null
  run_type: JobKind
  total: number
  limit: number
  offset: number
  items: RunListItem[]
}

// ---- 发布预检（固定 8 步骤，前端按版本与本次选择过滤） ----

export type PublishStepName =
  | 'db'
  | 'ddl'
  | 'json'
  | 'domain'
  | 'table_schema'
  | 'dim_value'
  | 'rel'
  | 'sql'

export interface PublishCheckStep {
  step_name: PublishStepName
  ready: boolean
  reason: string | null
}

export interface PublishCheckDirectory {
  directory: string
  /** success | failed | none */
  marker_status: string
  collection_run_id: string | null
  /** ISO 文本（上海时区，+08:00）；无 marker 时为 null */
  collected_at: string | null
  error: string | null
}

export interface PublishCheckResponse {
  workspace_id: string
  source_id: string
  steps: PublishCheckStep[]
  input_directories: PublishCheckDirectory[]
}

// ---- 数据资产 ----

export interface AssetCatalogItem {
  publish_run_id: string
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
  published_at: string | null
}

export interface AssetCatalogResponse {
  source_count: number
  object_count: number
  project_count: number
  items: AssetCatalogItem[]
}

export type AssetProfileState = 'ready' | 'not_published'

export interface AssetProfileSource {
  source_id: string
  display_name: string
  username: string
}

export interface AssetProfilePublish {
  publish_run_id: string
  status: string
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

export interface DuplicateSourceDetail {
  source_id: string
  display_name: string
}

export interface ApiErrorResponse {
  code: string
  message: string
  details?: unknown
}

/** 发布创建前集中预检失败时 details 中的步骤原因（doc 6.2 §6.4；doc 7.2 起可带既有业务码） */
export interface PublishPrecheckFailure {
  step: string
  reason: string
  /** 既有业务错误码（如 dim_value_empty_tables）；无则不返回 */
  code?: string | null
}

// ---- 对象画像读取（doc 19.2 §8.1.1；publish 来源改 publish_run_id） ----

export type ObjectState = 'ready' | 'not_published' | 'inconsistent'
export type PrimaryKeyState = 'present' | 'none' | 'unknown'

export interface JsonPublishInfo {
  publish_run_id: string
  published_at: string | null
}

export interface DdlPublishInfo {
  publish_run_id: string
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
  publish_run_id: string
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
  publish_run_id: string
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
  json_publish_run_id: string | null
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

/** 文件 GET/PUT 的配置值形态（doc 7.2 §3.1.1）：单列字符串、字段列表或 null */
export interface DimValueConfigTable {
  schema_name: string
  object_name: string
  embedding_col: string | string[] | null
}

/** 只读推荐信息（doc 7.1 §4.4）：来自当前工作区 JSON 画像，不参与保存提交 */
export interface DimValueRecommendation {
  dimension_value_columns: string[]
  dimension_value_confidence: number | null
  recommendation_available: boolean
}

export interface DimValueConfigGetTable extends DimValueConfigTable, DimValueRecommendation {}

export interface DimValueConfigGetResponse {
  tables: DimValueConfigGetTable[]
  version: string
  /** embedding_col 为含逗号字符串的表（读取按未指定处理；显式初始化页据此
      提示改用字段列表格式，doc 7.2 §3.1.1/验收 25） */
  comma_string_tables: string[]
}

export interface DimValueConfigUpdateRequest {
  tables: DimValueConfigTable[]
  version: string
}

export interface DimValueConfigUpdateResponse {
  tables: DimValueConfigTable[]
}

/**
 * 草稿/版本/运行参数的 dim_value 表项（doc 7.2 §3.1）：embedding_col 统一为
 * 字段名数组（单字段也是单元素数组）；推荐信息不进入执行配置。
 */
export interface DimValueTableParam {
  schema_name: string
  object_name: string
  embedding_col: string[]
}

/** 发布 domain 运行/版本参数项（doc 7.2 §7.2）；与文件接口的域形状一致 */
export type DomainParam = DomainConfigDomain

/** 独立推荐读取（doc 7.2 §4）：与被编辑 YAML 解耦的只读能力 */
export interface DimValueRecommendationItem extends DimValueRecommendation {
  schema_name: string
  object_name: string
}

export interface DimValueRecommendationsResponse {
  tables: DimValueRecommendationItem[]
}

/** 作业级预检（doc 7.2 §5.2）：与创建运行同一解析规则，不创建运行 */
export interface PublishJobPrecheckRequest {
  revision_no: number
  mode: RunMode
  steps?: string[] | null
  step_config?: Record<string, Record<string, unknown>> | null
}

export interface PublishJobPrecheckStep {
  step_name: string
  ready: boolean
  reason: string | null
  code: string | null
}

export interface PublishJobPrecheckResponse {
  publish_job_id: string
  revision_no: number
  mode: RunMode
  steps: PublishJobPrecheckStep[]
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

// ---- rel 关系编辑（doc 23.2 §4） ----

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
