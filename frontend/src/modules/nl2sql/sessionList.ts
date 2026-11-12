/**
 * 智能问数首页与历史对话框各自持有的会话列表。
 * 请求代隔离过期响应；加载更多使用服务端 next_offset，展示去重不改变偏移。
 */
import { ref } from 'vue'
import { Nl2sqlApiError, Nl2sqlNotConfiguredError, listSessions } from './api/client'
import type { SessionInfo } from './api/types'
import { messageForNl2sql } from './messages'

export interface SessionListQuery {
  sourceId?: string
  q?: string
}

const PAGE_SIZE = 30

export function createSessionList(pageSize = PAGE_SIZE) {
  const sessions = ref<SessionInfo[]>([])
  const loading = ref(false)
  const error = ref('')
  const hasMore = ref(false)
  const nextOffset = ref<number | null>(null)
  const totalAll = ref<number | null>(null)
  const totalFiltered = ref<number | null>(null)
  const totalTurnsAll = ref<number | null>(null)
  let generation = 0
  let currentQuery: SessionListQuery = {}

  function dedupe(items: SessionInfo[]): SessionInfo[] {
    const seen = new Set<string>()
    return items.filter((item) => {
      if (seen.has(item.session_id)) return false
      seen.add(item.session_id)
      return true
    })
  }

  async function loadFirst(query: SessionListQuery = {}) {
    const gen = ++generation
    currentQuery = { sourceId: query.sourceId, q: query.q }
    loading.value = true
    error.value = ''
    sessions.value = []
    hasMore.value = false
    nextOffset.value = null
    totalAll.value = null
    totalFiltered.value = null
    totalTurnsAll.value = null
    try {
      const page = await listSessions({
        sourceId: currentQuery.sourceId,
        q: currentQuery.q,
        limit: PAGE_SIZE,
        offset: 0,
      })
      if (gen !== generation) return
      sessions.value = dedupe(page.items)
      hasMore.value = page.has_more
      nextOffset.value = page.next_offset
      totalAll.value = page.total_all
      totalFiltered.value = page.total_filtered
      totalTurnsAll.value = page.total_turns_all
    } catch (caught) {
      if (gen !== generation) return
      error.value =
        caught instanceof Nl2sqlApiError || caught instanceof Nl2sqlNotConfiguredError
          ? messageForNl2sql(caught)
          : '会话列表读取失败，请稍后重试。'
    } finally {
      if (gen === generation) loading.value = false
    }
  }

  /** 首页按页替换列表。超出末页时改读末页，并返回实际页码。 */
  async function loadPage(query: SessionListQuery, pageNumber: number): Promise<number | null> {
    const gen = ++generation
    const requested = Number.isInteger(pageNumber) && pageNumber > 0 ? pageNumber : 1
    currentQuery = { sourceId: query.sourceId, q: query.q }
    loading.value = true
    error.value = ''
    sessions.value = []
    hasMore.value = false
    nextOffset.value = null
    totalAll.value = null
    totalFiltered.value = null
    totalTurnsAll.value = null
    try {
      const first = await listSessions({
        sourceId: currentQuery.sourceId,
        q: currentQuery.q,
        limit: pageSize,
        offset: (requested - 1) * pageSize,
      })
      if (gen !== generation) return null
      const last = Math.max(1, Math.ceil(first.total_filtered / pageSize))
      const applied = Math.min(requested, last)
      const page =
        applied === requested
          ? first
          : await listSessions({
              sourceId: currentQuery.sourceId,
              q: currentQuery.q,
              limit: pageSize,
              offset: (applied - 1) * pageSize,
            })
      if (gen !== generation) return null
      sessions.value = dedupe(page.items)
      hasMore.value = page.has_more
      nextOffset.value = page.next_offset
      totalAll.value = page.total_all
      totalFiltered.value = page.total_filtered
      totalTurnsAll.value = page.total_turns_all
      return applied
    } catch (caught) {
      if (gen !== generation) return null
      error.value =
        caught instanceof Nl2sqlApiError || caught instanceof Nl2sqlNotConfiguredError
          ? messageForNl2sql(caught)
          : '会话列表读取失败，请稍后重试。'
      return null
    } finally {
      if (gen === generation) loading.value = false
    }
  }

  async function loadMore() {
    if (!hasMore.value || loading.value || nextOffset.value === null) return
    const gen = generation
    const offset = nextOffset.value
    loading.value = true
    error.value = ''
    try {
      const page = await listSessions({
        sourceId: currentQuery.sourceId,
        q: currentQuery.q,
        limit: PAGE_SIZE,
        offset,
      })
      if (gen !== generation) return
      const seen = new Set(sessions.value.map((item) => item.session_id))
      sessions.value = [
        ...sessions.value,
        ...page.items.filter((item) => {
          if (seen.has(item.session_id)) return false
          seen.add(item.session_id)
          return true
        }),
      ]
      hasMore.value = page.has_more
      nextOffset.value = page.next_offset
      totalAll.value = page.total_all
      totalFiltered.value = page.total_filtered
      totalTurnsAll.value = page.total_turns_all
    } catch {
      if (gen !== generation) return
      error.value = '更多会话加载失败，请稍后重试。'
    } finally {
      if (gen === generation) loading.value = false
    }
  }

  return {
    sessions,
    loading,
    error,
    hasMore,
    nextOffset,
    totalAll,
    totalFiltered,
    totalTurnsAll,
    loadFirst,
    loadPage,
    loadMore,
  }
}
