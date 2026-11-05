// 工作区、作业、版本、运行与配置编辑接口（doc 6.2 §6.1；backend/api/routers/workspaces.py）
import { apiRequest } from './http'
import type {
  DomainConfigResponse,
  DomainConfigTablesResponse,
  DomainConfigUpdateRequest,
  DimValueColumnsResponse,
  DimValueConfigGetResponse,
  DimValueConfigUpdateRequest,
  DimValueConfigUpdateResponse,
  DimValueObjectsResponse,
  DimValueRecommendationsResponse,
  GlobalJobListResponse,
  JobCreateRequest,
  JobItem,
  JobKind,
  JobListResponse,
  JobPatchRequest,
  PublishCheckResponse,
  PublishJobPrecheckRequest,
  PublishJobPrecheckResponse,
  RelConfigColumnsResponse,
  RelConfigGetResponse,
  RelConfigUpdateRequest,
  RelConfigUpdateResponse,
  RevisionItem,
  RevisionListResponse,
  RunCreateRequest,
  RunDetail,
  RunListItem,
  RunListResponse,
  WorkspaceCreateRequest,
  WorkspaceItem,
  WorkspacePageResponse,
  WorkspacePatchRequest,
  DeletionTaskItem,
  WorkspaceDeletionPreview,
} from './types'


// ---- 工作区 ----

// 源内列表 GET /api/data-sources/{id}/workspaces 为旧形状（{source_id, items}），
// 数据源详情页已改用工作区管理入口；跨源列表统一走下方分页接口。

export interface WorkspacePageQuery {
  /** 可选：按数据源短 ID 过滤 */
  source_id?: string
  limit?: number
  offset?: number
}

/**
 * 跨源工作区列表（doc 6.2 §6.1：`GET /api/workspaces?source_id=&limit=&offset=`，
 * 服务端分页返回 items/total/limit/offset）。全局工作区管理页使用；
 * 不允许前端逐源拉取后拼接分页。
 */
export function listWorkspaces(query: WorkspacePageQuery = {}) {
  const search = new URLSearchParams()
  if (query.source_id) search.set('source_id', query.source_id)
  if (query.limit !== undefined) search.set('limit', String(query.limit))
  if (query.offset !== undefined) search.set('offset', String(query.offset))
  const text = search.toString()
  return apiRequest<WorkspacePageResponse>(
    `/api/workspaces${text ? `?${text}` : ''}`,
    {},
    'create',
    true,
  )
}

export function createSourceWorkspace(sourceId: string, payload: WorkspaceCreateRequest) {
  return apiRequest<WorkspaceItem>(`/api/data-sources/${sourceId}/workspaces`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function getWorkspace(workspaceId: string) {
  return apiRequest<WorkspaceItem>(`/api/workspaces/${workspaceId}`, {}, 'edit')
}

export function patchWorkspace(workspaceId: string, payload: WorkspacePatchRequest) {
  return apiRequest<WorkspaceItem>(`/api/workspaces/${workspaceId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

// ---- 作业与版本 ----

function jobBase(kind: JobKind) {
  return kind === 'collection' ? '/api/collection-jobs' : '/api/publish-jobs'
}

export function listJobs(kind: JobKind, workspaceId: string) {
  const path =
    kind === 'collection'
      ? `/api/workspaces/${workspaceId}/collection-jobs`
      : `/api/workspaces/${workspaceId}/publish-jobs`
  return apiRequest<JobListResponse>(path)
}

// ---- 全局作业列表（设计稿 35/37；跨工作区分页） ----

export interface GlobalJobListQuery {
  workspaceId?: string
  sourceId?: string
  q?: string
  limit?: number
  offset?: number
}

export function listJobsGlobal(kind: JobKind, query: GlobalJobListQuery = {}) {
  const search = new URLSearchParams()
  if (query.workspaceId) search.set('workspace_id', query.workspaceId)
  if (query.sourceId) search.set('source_id', query.sourceId)
  if (query.q) search.set('q', query.q)
  if (query.limit !== undefined) search.set('limit', String(query.limit))
  if (query.offset !== undefined) search.set('offset', String(query.offset))
  const text = search.toString()
  const path = kind === 'collection' ? '/api/collection-jobs' : '/api/publish-jobs'
  return apiRequest<GlobalJobListResponse>(`${path}${text ? `?${text}` : ''}`)
}

export function createJob(kind: JobKind, workspaceId: string, payload: JobCreateRequest) {
  const path =
    kind === 'collection'
      ? `/api/workspaces/${workspaceId}/collection-jobs`
      : `/api/workspaces/${workspaceId}/publish-jobs`
  return apiRequest<JobItem>(path, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function getJob(kind: JobKind, jobId: string) {
  return apiRequest<JobItem>(`${jobBase(kind)}/${jobId}`)
}

export function patchJob(kind: JobKind, jobId: string, payload: JobPatchRequest) {
  return apiRequest<JobItem>(`${jobBase(kind)}/${jobId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function listRevisions(kind: JobKind, jobId: string) {
  return apiRequest<RevisionListResponse>(`${jobBase(kind)}/${jobId}/revisions`)
}

export function addRevision(kind: JobKind, jobId: string, payload: JobCreateRequest) {
  return apiRequest<RevisionItem>(`${jobBase(kind)}/${jobId}/revisions`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

// ---- 运行 ----

export function createRun(kind: JobKind, jobId: string, payload: RunCreateRequest) {
  const path =
    kind === 'collection'
      ? `/api/collection-jobs/${jobId}/runs`
      : `/api/publish-jobs/${jobId}/runs`
  return apiRequest<{
    collection_run_id?: string
    publish_run_id?: string
    steps: string[]
    revision_no: number
    status: string
  }>(path, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function getRunDetail(kind: JobKind, runId: string) {
  const path =
    kind === 'collection' ? `/api/collection-runs/${runId}` : `/api/publish-runs/${runId}`
  return apiRequest<RunDetail>(path, {}, 'create', true)
}

export interface RunPageQuery {
  limit?: number
  offset?: number
}

function pageQuery(query: RunPageQuery): string {
  const search = new URLSearchParams()
  if (query.limit !== undefined) search.set('limit', String(query.limit))
  if (query.offset !== undefined) search.set('offset', String(query.offset))
  const text = search.toString()
  return text ? `?${text}` : ''
}

export function listJobRuns(kind: JobKind, jobId: string, query: RunPageQuery = {}) {
  return apiRequest<RunListResponse>(`${jobBase(kind)}/${jobId}/runs${pageQuery(query)}`)
}

export function listWorkspaceRuns(kind: JobKind, workspaceId: string, query: RunPageQuery = {}) {
  const path =
    kind === 'collection'
      ? `/api/workspaces/${workspaceId}/collection-runs`
      : `/api/workspaces/${workspaceId}/publish-runs`
  return apiRequest<RunListResponse>(`${path}${pageQuery(query)}`)
}

/** 运行列表项归一取运行 ID：工作区列表返回 run_id，作业运行历史返回具体键。 */
export function runIdOf(item: RunListItem): string {
  return item.run_id ?? item.collection_run_id ?? item.publish_run_id ?? ''
}

// ---- 发布预检 ----

/** 工作区级只读检查（固定全部 8 步骤；dim/domain 为准入最小集，不校验有效配置） */
export function getPublishCheck(workspaceId: string) {
  return apiRequest<PublishCheckResponse>(
    `/api/workspaces/${workspaceId}/publish-check`,
    {},
    'create',
    true,
  )
}

/**
 * 作业级预检（doc 7.2 §5.2）：按"引用版本默认值 + 本次允许的覆盖"解析
 * 有效参数并逐步骤校验，与创建运行同一规则；不创建运行。
 */
export function precheckPublishJob(jobId: string, payload: PublishJobPrecheckRequest) {
  return apiRequest<PublishJobPrecheckResponse>(
    `/api/publish-jobs/${jobId}/precheck`,
    { method: 'POST', body: JSON.stringify(payload) },
    'create',
    true,
  )
}

// ---- 配置编辑（工作区定位，doc 6.2 §6.1「文件编辑」） ----

export function getDomainConfig(workspaceId: string) {
  return apiRequest<DomainConfigResponse>(
    `/api/workspaces/${workspaceId}/domain-config`,
    {},
    'create',
    true,
  )
}

export function getDomainTables(workspaceId: string) {
  return apiRequest<DomainConfigTablesResponse>(
    `/api/workspaces/${workspaceId}/domain-config/tables`,
    {},
    'create',
    true,
  )
}

export function putDomainConfig(workspaceId: string, payload: DomainConfigUpdateRequest) {
  return apiRequest<DomainConfigResponse>(
    `/api/workspaces/${workspaceId}/domain-config`,
    { method: 'PUT', body: JSON.stringify(payload) },
    'create',
    true,
  )
}

export function getDimValueConfig(workspaceId: string) {
  return apiRequest<DimValueConfigGetResponse>(`/api/workspaces/${workspaceId}/dim-config`)
}

export function putDimValueConfig(
  workspaceId: string,
  payload: DimValueConfigUpdateRequest,
) {
  return apiRequest<DimValueConfigUpdateResponse>(
    `/api/workspaces/${workspaceId}/dim-config`,
    { method: 'PUT', body: JSON.stringify(payload) },
    'create',
    true,
  )
}

export function getDimValueObjects(workspaceId: string) {
  return apiRequest<DimValueObjectsResponse>(`/api/workspaces/${workspaceId}/dim-config/objects`)
}

/** 独立推荐读取（doc 7.2 §4）：与被编辑 YAML 解耦，草稿弹窗专用 */
export function getDimValueRecommendations(workspaceId: string) {
  return apiRequest<DimValueRecommendationsResponse>(
    `/api/workspaces/${workspaceId}/dim-config/recommendations`,
    {},
    'create',
    true,
  )
}

export function getDimValueObjectColumns(
  workspaceId: string,
  schemaName: string,
  objectName: string,
) {
  return apiRequest<DimValueColumnsResponse>(
    `/api/workspaces/${workspaceId}/dim-config/object-columns` +
      `?schema_name=${encodeURIComponent(schemaName)}&object_name=${encodeURIComponent(objectName)}`,
  )
}

export function getRelConfig(workspaceId: string) {
  return apiRequest<RelConfigGetResponse>(`/api/workspaces/${workspaceId}/rel-config`)
}

export function putRelConfig(workspaceId: string, payload: RelConfigUpdateRequest) {
  return apiRequest<RelConfigUpdateResponse>(
    `/api/workspaces/${workspaceId}/rel-config`,
    { method: 'PUT', body: JSON.stringify(payload) },
    'create',
    true,
  )
}

export function getRelObjectColumns(
  workspaceId: string,
  schemaName: string,
  objectName: string,
) {
  return apiRequest<RelConfigColumnsResponse>(
    `/api/workspaces/${workspaceId}/rel-config/object-columns` +
      `?schema_name=${encodeURIComponent(schemaName)}&object_name=${encodeURIComponent(objectName)}`,
  )
}

// ---- 删除（doc 8.1 §5.2/§7） ----

export function deleteWorkspace(workspaceId: string, confirmId: string) {
  return apiRequest<DeletionTaskItem>(`/api/workspaces/${workspaceId}`, {
    method: 'DELETE',
    body: JSON.stringify({ confirm_id: confirmId }),
  })
}

export function getWorkspaceDeletionPreview(workspaceId: string) {
  return apiRequest<WorkspaceDeletionPreview>(
    `/api/workspaces/${workspaceId}/deletion-preview`,
  )
}

export function deleteJob(kind: 'collection' | 'publish', jobId: string) {
  const path = kind === 'collection' ? 'collection-jobs' : 'publish-jobs'
  return apiRequest<void>(`/api/${path}/${jobId}`, { method: 'DELETE' })
}
