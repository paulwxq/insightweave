/**
 * NL2SQL 查询页状态与请求协调（doc 2 §7.1）。
 *
 * 页面状态：未配置 → 加载数据源与会话 → 选源新建或恢复会话 → 输入 →
 * 提交中 → 完成/部分完成/待澄清/无数据/失败。同步 API 无逐步进度，
 * 提交时仅显示等待与已耗时；用会话 ID + 请求令牌丢弃迟到响应。
 */

import { computed, onUnmounted, ref, shallowRef } from 'vue'
import type { Router } from 'vue-router'
import { listDataSources } from '../../../api/dataSources'
import { ApiError } from '../../../api/http'
import type { DataSourceListItem } from '../../../api/types'
import {
  Nl2sqlApiError,
  Nl2sqlNotConfiguredError,
  createSession,
  getSession,
  getReadiness,
  listTurns,
  submitQuery,
} from '../api/client'
import type { ReadinessData, SessionInfo, WebQueryResult, WebTurn } from '../api/types'
import { NL2SQL_MESSAGES, messageForNl2sql } from '../messages'
import {
  activeSessionId,
  loadMoreSessions,
  refreshSessions,
  sessions,
  sessionsError,
  sessionsHasMore,
  sessionsLoading,
  sessionsNotice,
} from '../sessionList'

export { NL2SQL_MESSAGES, messageForNl2sql }

/** 令会话后续提交被拒绝的 error_code（doc 2 §7.2）。 */
const SESSION_DEAD_CODES = new Set([
  'session_invalidated',
  'source_changed',
  'knowledge_changed',
  'session_binding_missing',
  'source_not_found',
  'source_inactive',
])

export type AskState = 'idle' | 'submitting' | 'done' | 'error'

export interface CurrentTurn {
  question: string
  result: WebQueryResult | null
  error: Nl2sqlApiError | null
  errorMessage: string
}

export type ReadinessState = 'idle' | 'checking' | 'ready' | 'blocked' | 'error'

export function useNl2sqlChat(router: Router) {
  // ------------------------------------------------------------- 数据源（平台 API）
  const sources = ref<DataSourceListItem[]>([])
  const sourcesLoading = ref(false)
  const sourcesError = ref('')

  // ------------------------------------------------------------- 选源与 readiness
  const selectedSourceId = ref('')
  const readinessState = ref<ReadinessState>('idle')
  const readinessData = shallowRef<ReadinessData | null>(null)
  const readinessError = ref('')

  // ------------------------------------------------------------- 当前会话
  const currentSessionId = ref('')
  const sessionInfo = shallowRef<SessionInfo | null>(null)
  const sessionLoadError = ref('')
  const /** 409/session_metadata_invalid 持续时的只读历史模式（doc 2 §7.1）。 */
    readonlyHistory = ref(false)
  const turns = ref<WebTurn[]>([])
  const turnsLoading = ref(false)
  const turnsError = ref('')
  /** 已加载最早一轮在正序全量中的 offset（>0 表示还可加载更早）。 */
  const turnsOffset = ref(0)

  // ------------------------------------------------------------- 提问
  const askState = ref<AskState>('idle')
  const currentTurn = shallowRef<CurrentTurn | null>(null)
  const elapsedSeconds = ref(0)
  const askNotice = ref('')

  let requestToken = 0
  let elapsedTimer: ReturnType<typeof setInterval> | null = null

  const activeSources = computed(() => sources.value.filter((item) => item.is_active))

  /** 输入禁用原因；空字符串表示可提交。 */
  const inputBlockedReason = computed(() => {
    if (readonlyHistory.value) return NL2SQL_MESSAGES.session_metadata_invalid
    const info = sessionInfo.value
    if (info === null) {
      // 详情未加载完成（或加载失败）期间禁提，防止短窗口误提交
      return sessionLoadError.value ? '会话详情未能加载，请刷新重试。' : '会话详情加载中，请稍候。'
    }
    if (info.state === 'invalidated') {
      return info.invalid_reason === 'knowledge_changed'
        ? NL2SQL_MESSAGES.knowledge_changed
        : '会话已失效，请新建会话。'
    }
    if (!info.continuable) return '上一操作尚未确认，请刷新查看。'
    return ''
  })

  const canSubmit = computed(
    () =>
      currentSessionId.value !== '' &&
      inputBlockedReason.value === '' &&
      askState.value !== 'submitting',
  )

  function stopElapsed() {
    if (elapsedTimer !== null) {
      clearInterval(elapsedTimer)
      elapsedTimer = null
    }
  }

  onUnmounted(stopElapsed)

  // ------------------------------------------------------------- 数据源与列表

  async function loadSources() {
    sourcesLoading.value = true
    sourcesError.value = ''
    try {
      const payload = await listDataSources()
      sources.value = payload.items
    } catch (error) {
      sourcesError.value =
        error instanceof ApiError ? error.message : '数据源列表加载失败，请稍后重试。'
    } finally {
      sourcesLoading.value = false
    }
  }

  // ------------------------------------------------------------- 选源 readiness

  let readinessToken = 0

  async function checkReadiness(sourceId: string) {
    const token = ++readinessToken
    selectedSourceId.value = sourceId
    readinessState.value = 'checking'
    readinessError.value = ''
    readinessData.value = null
    try {
      const data = await getReadiness(sourceId)
      // 迟到响应不得覆盖新选择的源状态
      if (token !== readinessToken || selectedSourceId.value !== sourceId) return
      readinessData.value = data
      readinessState.value = data.ready ? 'ready' : 'blocked'
    } catch (error) {
      if (token !== readinessToken || selectedSourceId.value !== sourceId) return
      readinessState.value = 'error'
      readinessError.value =
        error instanceof Nl2sqlApiError || error instanceof Nl2sqlNotConfiguredError
          ? messageForNl2sql(error)
          : '就绪检查失败，请稍后重试。'
    }
  }

  function resetReadiness() {
    readinessToken += 1
    selectedSourceId.value = ''
    readinessState.value = 'idle'
    readinessData.value = null
    readinessError.value = ''
  }

  // ------------------------------------------------------------- 会话打开

  function resetCurrentSession() {
    requestToken += 1
    stopElapsed()
    currentSessionId.value = ''
    sessionInfo.value = null
    sessionLoadError.value = ''
    readonlyHistory.value = false
    turns.value = []
    turnsError.value = ''
    turnsOffset.value = 0
    askState.value = 'idle'
    currentTurn.value = null
    askNotice.value = ''
    activeSessionId.value = ''
  }

  /** 历史分页页大小：正序接口按“尾部一页 + 向前加载更早”使用。 */
  const TURNS_PAGE_SIZE = 50

  const turnsHasEarlier = computed(() => turnsOffset.value > 0)

  /** 加载最近的历史页：缓存计数仅作定位提示——计数偏大回退首页、
   * 计数偏小沿 has_more 继续取到真正尾页，保证最新问答总在已加载页内。 */
  async function loadTurns(sessionId: string, token: number) {
    turnsLoading.value = true
    turnsError.value = ''
    try {
      const total = sessionInfo.value?.turn_count ?? 0
      const offset = Math.max(0, total - TURNS_PAGE_SIZE)
      let page = await listTurns(sessionId, TURNS_PAGE_SIZE, offset)
      if (page.items.length === 0 && offset > 0) {
        page = await listTurns(sessionId, TURNS_PAGE_SIZE, 0)
      }
      while (page.has_more && page.next_offset !== null) {
        const next = await listTurns(sessionId, TURNS_PAGE_SIZE, page.next_offset)
        page = { ...next, offset: page.offset, items: [...page.items, ...next.items] }
      }
      if (token !== requestToken || currentSessionId.value !== sessionId) return
      turns.value = page.items
      turnsOffset.value = page.offset
    } catch (error) {
      if (token !== requestToken || currentSessionId.value !== sessionId) return
      turnsError.value =
        error instanceof Nl2sqlApiError || error instanceof Nl2sqlNotConfiguredError
          ? messageForNl2sql(error)
          : '历史读取失败，请稍后重试。'
    } finally {
      turnsLoading.value = false
    }
  }

  /** 向前加载更早的历史轮（只取缺失区间，不与已加载部分重叠）。 */
  async function loadEarlierTurns() {
    const sessionId = currentSessionId.value
    if (sessionId === '' || turnsLoading.value || turnsOffset.value <= 0) return
    turnsLoading.value = true
    try {
      const take = Math.min(TURNS_PAGE_SIZE, turnsOffset.value)
      const offset = turnsOffset.value - take
      const page = await listTurns(sessionId, take, offset)
      if (currentSessionId.value !== sessionId) return
      turnsOffset.value = offset
      turns.value = [...page.items, ...turns.value]
    } catch (error) {
      turnsError.value =
        error instanceof Nl2sqlApiError || error instanceof Nl2sqlNotConfiguredError
          ? messageForNl2sql(error)
          : '更早问答加载失败，请稍后重试。'
    } finally {
      turnsLoading.value = false
    }
  }

  /** 打开会话：详情（含 metadata_invalid 单次重读）→ turns（doc 2 §7.1）。
   * 每次异步写入前核对令牌与会话，迟到响应不覆盖新页面状态。 */
  async function openSession(sessionId: string) {
    resetCurrentSession()
    currentSessionId.value = sessionId
    activeSessionId.value = sessionId
    const token = requestToken
    const stale = () => token !== requestToken || currentSessionId.value !== sessionId
    try {
      const info = await getSession(sessionId)
      if (stale()) return
      sessionInfo.value = info
    } catch (error) {
      if (stale()) return
      if (!(error instanceof Nl2sqlApiError) || error.code !== 'session_metadata_invalid') {
        sessionLoadError.value =
          error instanceof Nl2sqlApiError || error instanceof Nl2sqlNotConfiguredError
            ? messageForNl2sql(error)
            : '会话详情读取失败，请稍后重试。'
        return
      }
      // 单次重读详情；仍为该错误码时进入只读历史模式
      try {
        const info = await getSession(sessionId)
        if (stale()) return
        sessionInfo.value = info
      } catch (retryError) {
        if (stale()) return
        if (
          retryError instanceof Nl2sqlApiError &&
          retryError.code === 'session_metadata_invalid'
        ) {
          readonlyHistory.value = true
        } else {
          sessionLoadError.value =
            retryError instanceof Nl2sqlApiError ||
            retryError instanceof Nl2sqlNotConfiguredError
              ? messageForNl2sql(retryError)
              : '会话详情读取失败，请稍后重试。'
        }
      }
    }
    if (stale()) return
    await loadTurns(sessionId, token)
  }

  // ------------------------------------------------------------- 新建会话

  async function createNewSession(): Promise<boolean> {
    if (selectedSourceId.value === '' || readinessState.value !== 'ready') return false
    sessionsNotice.value = ''
    try {
      const info = await createSession(selectedSourceId.value)
      await router.push({ path: `/query/${info.session_id}` })
      await refreshSessions()
      return true
    } catch (error) {
      if (
        error instanceof Nl2sqlApiError &&
        (error.code === 'timeout' || error.code === 'network_error')
      ) {
        // 创建结果未确认：刷新列表由用户选择，不自动重试（doc 2 §7.1）
        sessionsNotice.value = messageForNl2sql(error)
        await refreshSessions()
        return false
      }
      sessionsNotice.value =
        error instanceof Nl2sqlApiError || error instanceof Nl2sqlNotConfiguredError
          ? messageForNl2sql(error)
          : '会话创建失败，请重试。'
      return false
    }
  }

  function startNewConversation() {
    resetCurrentSession()
    resetReadiness()
    router.push({ path: '/query' })
  }

  /** 把刚保存的当前轮收成历史卡片。不安全终态不带回 SQL 和结果引用。 */
  function snapshotTurn(question: string, result: WebQueryResult): WebTurn {
    const unsafe =
      result.error_code === 'knowledge_changed' || result.error_code === 'dependency_unavailable'
    return {
      question,
      answer: result.answer,
      saved_at: new Date().toISOString(),
      elapsed_ms: result.stats.elapsed_ms,
      evidence: unsafe
        ? []
        : result.evidence.map((item) => ({
            sql: item.sql,
            result_ref: item.result_ref,
            row_count: item.row_count,
            source_truncated: item.source_truncated,
          })),
    }
  }

  // ------------------------------------------------------------- 提问

  async function refreshCurrentDetail(sessionId: string) {
    try {
      const info = await getSession(sessionId)
      if (currentSessionId.value === sessionId) sessionInfo.value = info
    } catch {
      // 刷新失败保留原状态；用户可手动刷新
    }
  }

  async function ask(question: string) {
    const sessionId = currentSessionId.value
    const sourceId = sessionInfo.value?.source_id
    if (!canSubmit.value || sessionId === '' || !sourceId) return
    const token = requestToken
    // 已保存的上一轮转入本地历史展示：历史接口只返回问答文本（D3，无轮次
    // 标识），不能凭尾页位置或文本判断"历史末条即当前轮"；重开会话时再
    // 以服务端为准全量加载
    const previous = currentTurn.value
    if (previous?.result?.history_saved === true && previous.result.answer) {
      turns.value = [...turns.value, snapshotTurn(previous.question, previous.result)]
    }
    askState.value = 'submitting'
    askNotice.value = ''
    currentTurn.value = { question, result: null, error: null, errorMessage: '' }
    elapsedSeconds.value = 0
    stopElapsed()
    elapsedTimer = setInterval(() => {
      elapsedSeconds.value += 1
    }, 1000)
    try {
      const result = await submitQuery(sessionId, question, sourceId)
      if (token !== requestToken || currentSessionId.value !== sessionId) return
      currentTurn.value = { question, result, error: null, errorMessage: '' }
      askState.value = 'done'
      // 不重读历史：已保存轮次在下次提问/重开会话时转入历史展示；
      // 只刷新详情（turn_count）与会话列表
      await Promise.allSettled([refreshCurrentDetail(sessionId), refreshSessions()])
    } catch (error) {
      if (token !== requestToken || currentSessionId.value !== sessionId) return
      const apiError =
        error instanceof Nl2sqlApiError
          ? error
          : new Nl2sqlApiError('internal_error', '请求失败，请稍后重试。')
      if (apiError.code === 'session_busy') {
        // 保留输入，不自动重试（doc 2 §7.2）
        askNotice.value = messageForNl2sql(apiError)
        currentTurn.value = null
        askState.value = 'idle'
        return
      }
      if (apiError.code === 'session_metadata_invalid') {
        // 立即禁用输入，重读一次详情决定只读或恢复（doc 2 §7.1）
        try {
          sessionInfo.value = await getSession(sessionId)
        } catch (retryError) {
          if (
            retryError instanceof Nl2sqlApiError &&
            retryError.code === 'session_metadata_invalid'
          ) {
            readonlyHistory.value = true
          }
        }
        askNotice.value = messageForNl2sql(apiError)
        currentTurn.value = { question, result: null, error: apiError, errorMessage: '' }
        askState.value = 'error'
        return
      }
      if (SESSION_DEAD_CODES.has(apiError.code)) {
        await refreshCurrentDetail(sessionId)
      }
      currentTurn.value = {
        question,
        result: (apiError.data as { result?: WebQueryResult } | null)?.result ?? null,
        error: apiError,
        errorMessage: messageForNl2sql(apiError),
      }
      askState.value = 'error'
    } finally {
      if (token === requestToken) stopElapsed()
    }
  }

  // ------------------------------------------------------------- 初始化

  async function initialize() {
    await Promise.allSettled([loadSources(), refreshSessions()])
  }

  return {
    // 数据源
    sources: activeSources,
    sourcesLoading,
    sourcesError,
    // 选源
    selectedSourceId,
    readinessState,
    readinessData,
    readinessError,
    checkReadiness,
    resetReadiness,
    // 会话列表
    sessions,
    sessionsLoading,
    sessionsError,
    sessionsHasMore,
    sessionsNotice,
    refreshSessions,
    loadMoreSessions,
    // 当前会话
    currentSessionId,
    sessionInfo,
    sessionLoadError,
    readonlyHistory,
    turns,
    turnsLoading,
    turnsError,
    turnsHasEarlier,
    loadEarlierTurns,
    inputBlockedReason,
    canSubmit,
    openSession,
    createNewSession,
    startNewConversation,
    resetCurrentSession,
    // 提问
    askState,
    currentTurn,
    elapsedSeconds,
    askNotice,
    ask,
    // 生命周期
    initialize,
  }
}
