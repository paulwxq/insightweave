import { ApiError, apiRequest } from './http'
import type {
  ConnectionCheckResponse,
  ConnectionFields,
  DataSourceCreateRequest,
  DataSourceListItem,
  DataSourceListResponse,
  DataSourceUpdateRequest,
  DeletionTaskDetail,
  DeletionTaskItem,
  DeletionTargetChange,
  DeletionTargetConfirmation,
  DeletionRunRef,
  SourceDeletionPreview,
} from './types'

/**
 * 从删除/清理被拒（409 resource_has_unfinished_runs）中提取阻断运行明细；
 * 其他错误返回空数组，调用方据此决定是否渲染清单。
 */
export function parseBlockingRuns(error: unknown): DeletionRunRef[] {
  if (!(error instanceof ApiError) || error.code !== 'resource_has_unfinished_runs') {
    return []
  }
  const details = error.details as { blocking_runs?: unknown } | undefined
  return Array.isArray(details?.blocking_runs) ? (details.blocking_runs as DeletionRunRef[]) : []
}

export function parseDeletionTargetChange(error: unknown): DeletionTargetChange | null {
  if (!(error instanceof ApiError) || error.code !== 'deletion_targets_changed') return null
  const details = error.details as DeletionTargetChange | undefined
  return details?.previous?.fingerprint && details.current?.fingerprint ? details : null
}

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

// ---- 删除与知识清理（doc 8.1 §2.4/§3.2/§7） ----

/** 提交源删除任务（202）：确认体回传目标 ID，替代旧 204/deleteOutput 契约。 */
export function deleteDataSource(sourceId: string, confirmId: string) {
  return apiRequest<DeletionTaskItem>(`/api/data-sources/${sourceId}`, {
    method: 'DELETE',
    body: JSON.stringify({ confirm_id: confirmId }),
  })
}

/** 源级知识清理（202）：清五张内容表 + 三类向量 + 该源图，保留作业与采集文件。 */
export function clearPublishedData(sourceId: string, confirmId: string) {
  return apiRequest<DeletionTaskItem>(`/api/data-sources/${sourceId}/published-data/clear`, {
    method: 'POST',
    body: JSON.stringify({ confirm_id: confirmId }),
  })
}

export function getSourceDeletionPreview(sourceId: string) {
  return apiRequest<SourceDeletionPreview>(`/api/data-sources/${sourceId}/deletion-preview`, {
    cache: 'no-store',
  })
}

export function getDeletionTask(taskId: string) {
  return apiRequest<DeletionTaskDetail>(`/api/deletion-tasks/${taskId}`)
}

export function retryDeletionTask(taskId: string, confirmation?: DeletionTargetConfirmation) {
  return apiRequest<DeletionTaskItem>(`/api/deletion-tasks/${taskId}/retry`, {
    method: 'POST',
    ...(confirmation ? { body: JSON.stringify({ target_confirmation: confirmation }) } : {}),
  })
}

export function updateDataSource(sourceId: string, fields: DataSourceUpdateRequest) {
  return apiRequest<DataSourceListItem>(
    `/api/data-sources/${sourceId}`,
    { method: 'PATCH', body: JSON.stringify(fields) },
    'edit',
  )
}
