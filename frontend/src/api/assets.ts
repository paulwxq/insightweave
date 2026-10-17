import { apiRequest } from './http'
import type { AssetCatalogResponse, AssetProfileResponse } from './types'

export function listAssets() {
  return apiRequest<AssetCatalogResponse>('/api/data-assets')
}

export function getAssetProfile(sourceId: string) {
  return apiRequest<AssetProfileResponse>(`/api/data-assets/${sourceId}/profile`)
}

import type {
  ObjectDdlResponse,
  ObjectDetailResponse,
  ObjectOptionsResponse,
  ObjectsResponse,
} from './types'

export interface ObjectListQuery {
  page?: number
  page_size?: number
  object_type?: string
  schema_name?: string
  keyword?: string
}

function buildQuery(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  const query = search.toString()
  return query ? `?${query}` : ''
}

export function listSourceObjects(sourceId: string, query: ObjectListQuery = {}) {
  return apiRequest<ObjectsResponse>(
    `/api/data-assets/${sourceId}/objects${buildQuery({ ...query })}`,
  )
}

export function listObjectOptions(
  sourceId: string,
  query: { object_type?: string; schema_name?: string; keyword?: string } = {},
) {
  return apiRequest<ObjectOptionsResponse>(
    `/api/data-assets/${sourceId}/object-options${buildQuery({ ...query })}`,
  )
}

export function getObjectDetail(
  sourceId: string,
  schemaName: string,
  objectName: string,
) {
  const query = buildQuery({ schema_name: schemaName, object_name: objectName })
  return apiRequest<ObjectDetailResponse>(`/api/data-assets/${sourceId}/object-detail${query}`)
}

export function getObjectDdl(sourceId: string, schemaName: string, objectName: string) {
  const query = buildQuery({ schema_name: schemaName, object_name: objectName })
  return apiRequest<ObjectDdlResponse>(`/api/data-assets/${sourceId}/object-ddl${query}`)
}
