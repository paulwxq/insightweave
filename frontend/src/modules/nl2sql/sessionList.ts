/**
 * 侧栏与查询页共用的会话列表。
 * 查询页负责加载；侧栏只读取同一份状态，避免两处各请求一份。
 */
import { ref } from 'vue'
import { Nl2sqlApiError, Nl2sqlNotConfiguredError, listSessions } from './api/client'
import type { SessionInfo } from './api/types'
import { messageForNl2sql } from './messages'

export const sessions = ref<SessionInfo[]>([])
export const sessionsLoading = ref(false)
export const sessionsError = ref('')
export const sessionsHasMore = ref(false)
export const sessionsNotice = ref('')
export const activeSessionId = ref('')

export async function refreshSessions() {
  sessionsLoading.value = true
  sessionsError.value = ''
  try {
    const page = await listSessions({ limit: 30 })
    sessions.value = page.items
    sessionsHasMore.value = page.has_more
  } catch (error) {
    sessionsError.value =
      error instanceof Nl2sqlApiError || error instanceof Nl2sqlNotConfiguredError
        ? messageForNl2sql(error)
        : '会话列表读取失败，请稍后重试。'
  } finally {
    sessionsLoading.value = false
  }
}

export async function loadMoreSessions() {
  if (!sessionsHasMore.value || sessionsLoading.value) return
  try {
    const page = await listSessions({ limit: 30, offset: sessions.value.length })
    sessions.value = [...sessions.value, ...page.items]
    sessionsHasMore.value = page.has_more
  } catch {
    sessionsError.value = '更多会话加载失败，请稍后重试。'
  }
}
