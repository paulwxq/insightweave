import { apiRequest } from './http'
import type {
  ConnectionCheckResponse,
  ConnectionFields,
  DataSourceCreateRequest,
  CollectionJobCreateResponse,
  CollectionJobListResponse,
  SqlReadinessResponse,
  CollectionHomeResponse,
  PublishDefinitionResponse,
  PublishHomeResponse,
  PublishJobListResponse,
  DataSourceListItem,
  PublicationCreateResponse,
  PublishCheckResponse,
  DataSourceListResponse,
  DataSourceUpdateRequest,
  DomainConfigResponse,
  DomainConfigTablesResponse,
  DomainConfigUpdateRequest,
  DimValueColumnsResponse,
  DimValueConfigGetResponse,
  DimValueConfigUpdateRequest,
  DimValueObjectsResponse,
  RelConfigColumnsResponse,
  RelConfigGetResponse,
  RelConfigUpdateRequest,
  RelConfigUpdateResponse,
} from './types'

export function checkConnection(fields: ConnectionFields) {
  return apiRequest<ConnectionCheckResponse>('/api/data-sources/connection-check', {
    method: 'POST',
    body: JSON.stringify(fields),
  })
}

export function createDataSource(fields: DataSourceCreateRequest) {
  return apiRequest<DataSourceListItem>('/api/data-sources', {
    method: 'POST',
    body: JSON.stringify(fields),
  })
}

export function listDataSources() {
  return apiRequest<DataSourceListResponse>('/api/data-sources')
}

export function getDataSource(sourceId: string) {
  return apiRequest<DataSourceListItem>(`/api/data-sources/${sourceId}`, {}, 'edit')
}

export function setDataSourceActivation(sourceId: string, isActive: boolean) {
  return apiRequest<DataSourceListItem>(
    `/api/data-sources/${sourceId}/activation`,
    { method: 'PATCH', body: JSON.stringify({ is_active: isActive }) },
    'edit',
  )
}

export function listCollectionJobs(sourceId: string) {
  return apiRequest<CollectionJobListResponse>(`/api/data-sources/${sourceId}/collection-jobs`)
}

export function listPublishJobs(sourceId: string) {
  return apiRequest<PublishJobListResponse>(`/api/data-sources/${sourceId}/publish-jobs`)
}

export function getCollectionHome(sourceId: string) {
  return apiRequest<CollectionHomeResponse>(
    `/api/data-sources/${sourceId}/collection-home`,
    {},
    'create',
    true,
  )
}

export function getPublishHome(sourceId: string) {
  return apiRequest<PublishHomeResponse>(
    `/api/data-sources/${sourceId}/publish-home`,
    {},
    'create',
    true,
  )
}

export function getPublishDefinition(sourceId: string, publishId: string) {
  return apiRequest<PublishDefinitionResponse>(
    `/api/data-sources/${sourceId}/publish-jobs/${publishId}`,
  )
}

export function checkPublication(sourceId: string, jobId: string) {
  return apiRequest<PublishCheckResponse>(
    `/api/data-sources/${sourceId}/collection-jobs/${jobId}/publish-check`,
    {},
    'create',
    true,
  )
}

export function createPublication(sourceId: string, jobId: string, steps: string[]) {
  return apiRequest<PublicationCreateResponse>(
    `/api/data-sources/${sourceId}/collection-jobs/${jobId}/publications`,
    { method: 'POST', body: JSON.stringify({ steps }) },
    'create',
    true,
  )
}

export function getDomainConfig(sourceId: string, jobId: string) {
  return apiRequest<DomainConfigResponse>(
    `/api/data-sources/${sourceId}/collection-jobs/${jobId}/domain-config`,
    {},
    'create',
    true,
  )
}

export function getDomainTables(sourceId: string, jobId: string) {
  return apiRequest<DomainConfigTablesResponse>(
    `/api/data-sources/${sourceId}/collection-jobs/${jobId}/domain-config/tables`,
    {},
    'create',
    true,
  )
}

export function putDomainConfig(
  sourceId: string,
  jobId: string,
  payload: DomainConfigUpdateRequest,
) {
  return apiRequest<DomainConfigResponse>(
    `/api/data-sources/${sourceId}/collection-jobs/${jobId}/domain-config`,
    { method: 'PUT', body: JSON.stringify(payload) },
    'create',
    true,
  )
}

export function getDimValueConfig(sourceId: string, jobId: string) {
  return apiRequest<DimValueConfigGetResponse>(
    `/api/data-sources/${sourceId}/collection-jobs/${jobId}/dim-config`,
  )
}

export function putDimValueConfig(
  sourceId: string,
  jobId: string,
  payload: DimValueConfigUpdateRequest,
) {
  return apiRequest<DimValueConfigGetResponse>(
    `/api/data-sources/${sourceId}/collection-jobs/${jobId}/dim-config`,
    { method: 'PUT', body: JSON.stringify(payload) },
    'create',
    true,
  )
}

export function getDimValueObjects(sourceId: string, jobId: string) {
  return apiRequest<DimValueObjectsResponse>(
    `/api/data-sources/${sourceId}/collection-jobs/${jobId}/dim-config/objects`,
  )
}

export function getDimValueObjectColumns(
  sourceId: string,
  jobId: string,
  schemaName: string,
  objectName: string,
) {
  return apiRequest<DimValueColumnsResponse>(
    `/api/data-sources/${sourceId}/collection-jobs/${jobId}/dim-config/object-columns` +
      `?schema_name=${encodeURIComponent(schemaName)}&object_name=${encodeURIComponent(objectName)}`,
  )
}

export function getRelConfig(sourceId: string, jobId: string) {
  return apiRequest<RelConfigGetResponse>(
    `/api/data-sources/${sourceId}/collection-jobs/${jobId}/rel-config`,
  )
}

export function putRelConfig(
  sourceId: string,
  jobId: string,
  payload: RelConfigUpdateRequest,
) {
  return apiRequest<RelConfigUpdateResponse>(
    `/api/data-sources/${sourceId}/collection-jobs/${jobId}/rel-config`,
    { method: 'PUT', body: JSON.stringify(payload) },
    'create',
    true,
  )
}

export function getRelObjectColumns(
  sourceId: string,
  jobId: string,
  schemaName: string,
  objectName: string,
) {
  return apiRequest<RelConfigColumnsResponse>(
    `/api/data-sources/${sourceId}/collection-jobs/${jobId}/rel-config/object-columns` +
      `?schema_name=${encodeURIComponent(schemaName)}&object_name=${encodeURIComponent(objectName)}`,
  )
}

export function createCollectionJob(
  sourceId: string,
  steps: string[],
  jobId = '',
  domainDescription?: string,
  syncDomainDescription = false,
) {
  // doc 21.1 §8.4：仅当本次会话保存过描述（domainDescription 已提供）时成对携带；
  // 未保存/未勾选 domain 时两字段都不发送，由后端按 db_desc → 数据源描述解析。
  const body: {
    steps: string[]
    job_id?: string
    domain_description?: string
    sync_domain_description?: boolean
  } = { steps }
  if (jobId) body.job_id = jobId
  if (domainDescription !== undefined) {
    body.domain_description = domainDescription
    body.sync_domain_description = syncDomainDescription
  }
  return apiRequest<CollectionJobCreateResponse>(
    `/api/data-sources/${sourceId}/collection-jobs`,
    { method: 'POST', body: JSON.stringify(body) },
    'create',
    true,
  )
}

export function getSqlReadiness(sourceId: string, jobId: string) {
  return apiRequest<SqlReadinessResponse>(
    `/api/data-sources/${sourceId}/collection-jobs/${jobId}/sql-readiness`,
    {},
    'create',
    true,
  )
}

export function checkSavedConnection(sourceId: string) {
  return apiRequest<ConnectionCheckResponse>(
    `/api/data-sources/${sourceId}/connection-check`,
    { method: 'POST' },
    'edit',
  )
}

export function deleteDataSource(sourceId: string, deleteOutput = false): Promise<void> {
  const query = deleteOutput ? '?delete_output=true' : ''
  return apiRequest<void>(`/api/data-sources/${sourceId}${query}`, { method: 'DELETE' })
}

export function updateDataSource(sourceId: string, fields: DataSourceUpdateRequest) {
  return apiRequest<DataSourceListItem>(
    `/api/data-sources/${sourceId}`,
    { method: 'PATCH', body: JSON.stringify(fields) },
    'edit',
  )
}
