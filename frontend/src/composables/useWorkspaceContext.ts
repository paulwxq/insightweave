/**
 * 工作区上下文加载：工作区详情 + 所属数据源（页面顶部始终显示来源与工作区，
 * doc 6.2 §9.1.1）。工作区或数据源不存在时跳回工作区管理列表并提示
 * （doc 6.3 §3.2）。
 */
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { getDataSource } from '../api/dataSources'
import { ApiError } from '../api/http'
import { getWorkspace } from '../api/workspaces'
import type { DataSourceListItem, WorkspaceItem } from '../api/types'

export function useWorkspaceContext(workspaceIdRef: () => string) {
  const router = useRouter()
  const workspace = ref<WorkspaceItem | null>(null)
  const source = ref<DataSourceListItem | null>(null)
  const loading = ref(true)
  const error = ref('')

  async function load() {
    const workspaceId = workspaceIdRef()
    if (!workspaceId) return
    loading.value = true
    error.value = ''
    workspace.value = null
    source.value = null
    try {
      workspace.value = await getWorkspace(workspaceId)
      source.value = await getDataSource(workspace.value.source_id)
    } catch (cause) {
      if (
        cause instanceof ApiError &&
        (cause.code === 'not_found' || cause.code === 'invalid_request')
      ) {
        // 工作区不存在 → 返回工作区管理列表并提示（doc 6.3 §3.2）
        await router.replace({ path: '/workspaces', query: { notice: 'not_found' } })
        return
      }
      error.value = cause instanceof Error ? cause.message : '工作区信息加载失败。'
    } finally {
      loading.value = false
    }
  }

  watch(workspaceIdRef, () => void load(), { immediate: true })

  const sourceDisabled = computed(() => source.value !== null && !source.value.is_active)

  return { workspace, source, loading, error, sourceDisabled, reload: load }
}
