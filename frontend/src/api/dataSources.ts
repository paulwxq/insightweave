import { apiRequest } from './http'
import type {
  ConnectionCheckResponse,
  ConnectionFields,
  DataSourceCreateRequest,
  DataSourceListItem,
  DataSourceListResponse,
  DataSourceUpdateRequest,
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
