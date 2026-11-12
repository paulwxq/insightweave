import { ref } from 'vue'
import { listDataSources } from '../../api/dataSources'
import { ApiError } from '../../api/http'
import type { DataSourceListItem } from '../../api/types'

export interface SourceChoice {
  value: string
  label: string
}

/**
 * 历史筛选使用全部已登记数据源。地址或对话框里已选的来源在列表未就绪时
 * 保留为临时选项，成功后用展示名替换，不保留重复项。
 */
export function useSourceCatalog() {
  const sources = ref<DataSourceListItem[]>([])
  const loading = ref(false)
  const error = ref('')
  const loaded = ref(false)

  async function load() {
    loading.value = true
    error.value = ''
    try {
      const payload = await listDataSources()
      sources.value = payload.items
      loaded.value = true
    } catch (caught) {
      sources.value = []
      loaded.value = false
      error.value = caught instanceof ApiError ? caught.message : '数据源列表加载失败，请稍后重试。'
    } finally {
      loading.value = false
    }
  }

  function find(sourceId: string): DataSourceListItem | undefined {
    return sources.value.find((item) => item.source_id === sourceId)
  }

  function choices(selectedId: string): SourceChoice[] {
    if (!loaded.value) return selectedId ? [{ value: selectedId, label: selectedId }] : []
    const items = sources.value.map((item) => ({
      value: item.source_id,
      label: item.is_active ? item.display_name : `${item.display_name}（已停用）`,
    }))
    if (!selectedId || items.some((item) => item.value === selectedId)) return items
    return [{ value: selectedId, label: selectedId }, ...items]
  }

  function databaseLabel(sourceId: string): string {
    if (!loaded.value) return sourceId
    return find(sourceId)?.display_name || sourceId
  }

  function inactive(sourceId: string): boolean {
    if (!loaded.value) return false
    const item = find(sourceId)
    return item !== undefined && !item.is_active
  }

  function canCreate(sourceId: string): boolean {
    return loaded.value && find(sourceId)?.is_active === true
  }

  return { sources, loading, error, loaded, load, choices, databaseLabel, inactive, canCreate }
}
