import { computed, onMounted, ref, watch } from 'vue'
import { listDataSources } from '../api/dataSources'
import { ApiError } from '../api/http'
import type { DataSourceListItem } from '../api/types'

export const HOME_PAGE_SIZE = 10

export interface HomeJob {
  job_id: string
  publish_id: string | null
  executed_at: string
  steps: string[]
  status: string
}

export interface HomePayload {
  is_active: boolean
  counts: Record<string, number>
  items: HomeJob[]
}

export interface HomeError {
  code: string
  message: string
}

export function useSourceHome(
  fetchHome: (sourceId: string) => Promise<HomePayload>,
  itemKey: (item: HomeJob) => string,
) {
  const sources = ref<DataSourceListItem[]>([])
  const sourcesError = ref('')
  const sourcesLoading = ref(true)
  const dbType = ref('')
  const selectedSourceId = ref('')
  const snapshot = ref<HomePayload | null>(null)
  const homeLoading = ref(false)
  const refreshing = ref(false)
  const homeError = ref<HomeError | null>(null)
  const sourceMissing = ref(false)
  const selectedKey = ref('')
  const sortDesc = ref(true)
  const page = ref(1)
  let requestSeq = 0
  let applyingSources = false

  const filteredSources = computed(() =>
    sources.value.filter(
      (item) => !dbType.value || item.db_type.toLowerCase() === dbType.value.toLowerCase(),
    ),
  )
  const selectedSource = computed(
    () => sources.value.find((item) => item.source_id === selectedSourceId.value) ?? null,
  )
  const sortedItems = computed(() => {
    const items = [...(snapshot.value?.items ?? [])]
    items.sort((left, right) => {
      const delta = left.executed_at.localeCompare(right.executed_at)
      return sortDesc.value ? -delta : delta
    })
    return items
  })
  const pageCount = computed(() => Math.max(1, Math.ceil(sortedItems.value.length / HOME_PAGE_SIZE)))
  const pageItems = computed(() => {
    const start = (page.value - 1) * HOME_PAGE_SIZE
    return sortedItems.value.slice(start, start + HOME_PAGE_SIZE)
  })
  const pageNumbers = computed(() =>
    Array.from({ length: pageCount.value }, (_, index) => index + 1),
  )
  const selectedItem = computed(
    () => pageItems.value.find((item) => itemKey(item) === selectedKey.value) ?? null,
  )
  const actionsReady = computed(
    () =>
      Boolean(selectedSource.value) &&
      !sourcesError.value &&
      !homeLoading.value &&
      !sourceMissing.value &&
      snapshot.value?.is_active === true,
  )

  function visibleKeepsSelection() {
    if (!selectedKey.value) return
    if (!pageItems.value.some((item) => itemKey(item) === selectedKey.value)) {
      selectedKey.value = ''
    }
  }

  function clampPage() {
    const pages = pageCount.value
    if (sortedItems.value.length === 0) page.value = 1
    else if (page.value > pages) page.value = pages
  }

  async function loadHome(mode: 'switch' | 'refresh') {
    const sourceId = selectedSourceId.value
    if (!sourceId) return
    const token = ++requestSeq
    if (mode === 'switch') {
      snapshot.value = null
      selectedKey.value = ''
      page.value = 1
      homeLoading.value = true
      homeError.value = null
      sourceMissing.value = false
    } else {
      refreshing.value = true
    }
    try {
      const payload = await fetchHome(sourceId)
      if (token !== requestSeq || sourceId !== selectedSourceId.value) return
      const row = sources.value.find((item) => item.source_id === sourceId)
      if (row) row.is_active = payload.is_active
      snapshot.value = payload
      sourceMissing.value = false
      homeError.value = null
      clampPage()
      visibleKeepsSelection()
    } catch (error) {
      if (token !== requestSeq || sourceId !== selectedSourceId.value) return
      const code = error instanceof ApiError ? error.code : 'network_error'
      const message = error instanceof ApiError ? error.message : '加载失败'
      homeError.value = { code, message }
      if (code === 'not_found') {
        sourceMissing.value = true
        selectedKey.value = ''
      }
    } finally {
      if (token === requestSeq) {
        homeLoading.value = false
        refreshing.value = false
      }
    }
  }

  function chooseSource(sourceId: string) {
    if (!sourceId || sourceId === selectedSourceId.value) return
    selectedSourceId.value = sourceId
    void loadHome('switch')
  }

  function clearIdleHome() {
    selectedSourceId.value = ''
    snapshot.value = null
    selectedKey.value = ''
    page.value = 1
    homeError.value = null
    sourceMissing.value = false
    homeLoading.value = false
    refreshing.value = false
  }

  async function reloadSources() {
    applyingSources = true
    sourcesLoading.value = true
    sourcesError.value = ''
    try {
      const response = await listDataSources()
      const previous = selectedSourceId.value
      sources.value = response.items
      const stillThere =
        previous !== '' && filteredSources.value.some((item) => item.source_id === previous)
      if (stillThere) {
        await loadHome('switch')
      } else if (filteredSources.value[0]) {
        selectedSourceId.value = ''
        chooseSource(filteredSources.value[0].source_id)
      } else {
        clearIdleHome()
      }
    } catch (error) {
      sourcesError.value = error instanceof ApiError ? error.message : '数据源列表加载失败。'
      clearIdleHome()
    } finally {
      sourcesLoading.value = false
      applyingSources = false
    }
  }

  function retryHome() {
    if (!snapshot.value) void loadHome('switch')
    else void loadHome('refresh')
  }

  function refreshHome() {
    if (!selectedSourceId.value || refreshing.value || homeLoading.value) return
    void loadHome('refresh')
  }

  function toggleSort() {
    sortDesc.value = !sortDesc.value
    page.value = 1
    selectedKey.value = ''
  }

  function goToPage(next: number) {
    if (next === page.value) return
    page.value = next
    selectedKey.value = ''
  }

  function selectItem(key: string) {
    selectedKey.value = key
  }

  watch(filteredSources, (list) => {
    if (applyingSources || sourcesError.value || sourcesLoading.value) return
    if (selectedSourceId.value && list.some((item) => item.source_id === selectedSourceId.value)) return
    if (list[0]) chooseSource(list[0].source_id)
    else clearIdleHome()
  })

  onMounted(() => {
    void reloadSources()
  })

  return {
    sources,
    sourcesError,
    sourcesLoading,
    dbType,
    filteredSources,
    selectedSourceId,
    selectedSource,
    snapshot,
    homeLoading,
    refreshing,
    homeError,
    sourceMissing,
    selectedKey,
    selectedItem,
    sortDesc,
    page,
    pageCount,
    sortedItems,
    pageItems,
    pageNumbers,
    actionsReady,
    chooseSource,
    reloadSources,
    retryHome,
    refreshHome,
    toggleSort,
    goToPage,
    selectItem,
  }
}
