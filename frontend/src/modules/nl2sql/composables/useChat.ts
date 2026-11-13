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
import { codepointLength, QUESTION_MAX } from '../format'
import { NL2SQL_MESSAGES, messageForNl2sql } from '../messages'
import { nl2sqlPageGuard } from '../pageGuard'
import { sessionBackTarget } from '../queryHomeLink'

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

/** 把刚保存的当前轮收成历史卡片。不安全终态不带回 SQL 和结果引用。
 *  模块级纯函数，便于对三态与安全投影做可执行行为测试（doc 6.3 §8.3）。 */
export function snapshotTurn(question: string, result: WebQueryResult): WebTurn {
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
    // 与服务端 WebStore 保存的同一合并列表保持一致（doc 6.3 §8.3）
    open_questions: unsafe ? [] : [...result.open_questions],
  }
}

export function useNl2sqlChat(router: Router) {
  const page = nl2sqlPageGuard.openInstance()
  function stillHere(): boolean {
    return page.stillHere()
  }
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
  const sessionsNotice = ref('')
  /** 受控首问草稿（doc 11）：创建失败或路由切换不丢输入。 */
  const questionDraft = ref('')
  /** 首次发送的创建阶段忙碌标记。 */
  const creatingSession = ref(false)

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

  /** 页面统一忙碌（doc 11）：创建中或查询执行中。 */
  const busy = computed(() => creatingSession.value || askState.value === 'submitting')

  /** 允许输入并发送：无会话看选库+就绪，有会话沿用既有可提交条件。 */
  const canCompose = computed(() => {
    if (busy.value) return false
    if (currentSessionId.value === '') {
      return selectedSourceId.value !== '' && readinessState.value === 'ready'
    }
    return canSubmit.value
  })

  function stopElapsed() {
    if (elapsedTimer !== null) {
      clearInterval(elapsedTimer)
      elapsedTimer = null
    }
  }

  onUnmounted(() => {
    stopElapsed()
    page.release()
  })

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
    // 更换来源即清上一次创建提示，避免旧错误遮住新校验结果（doc 11）
    sessionsNotice.value = ''
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
    sessionsNotice.value = ''
  }

  // ------------------------------------------------------------- 会话打开

  function resetCurrentSession() {
    requestToken += 1
    // 作废进行中的首次发送：同步释放创建忙碌，避免同页残留（doc 11 任务 2）
    creatingSession.value = false
    questionDraft.value = ''
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
   * 每次异步写入前核对令牌与会话，迟到响应不覆盖新页面状态。
   * 同一会话的重读（如“刷新对话”）保留未发送草稿；真正切换会话仍清空（Kimi R1-01）。 */
  async function openSession(sessionId: string) {
    const preservedDraft = currentSessionId.value === sessionId ? questionDraft.value : ''
    resetCurrentSession()
    if (preservedDraft !== '') questionDraft.value = preservedDraft
    currentSessionId.value = sessionId
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

  // ------------------------------------------------------------- 新建会话（doc 11：首次发送时创建）

  /** 创建结果不确定时的统一提示：网络失败也可能发生在请求送达之后，不能断言未创建。 */
  const CREATE_PENDING_NOTICE = '会话创建结果尚未确认，请勿重复发送，可稍后从查询历史查看。'

  function invalidatePendingCreate() {
    nl2sqlPageGuard.invalidateCreate()
    // 作废时同步释放创建忙碌，迟到 finally 不得清掉之后新发起的创建状态
    creatingSession.value = false
  }

  /** 当前页面是否已接管该会话：当前 ID 与真实 sessionInfo 均一致。 */
  function hasCurrentSession(sessionId: string): boolean {
    if (currentSessionId.value !== sessionId) return false
    const info = sessionInfo.value
    return info !== null && info.session_id === sessionId
  }

  /** 只创建会话，不导航：成功返回真实 SessionInfo，失败或过期返回 null。 */
  async function createSessionForFirstQuestion(): Promise<SessionInfo | null> {
    if (selectedSourceId.value === '' || readinessState.value !== 'ready') return null
    const token = requestToken
    const createdAt = nl2sqlPageGuard.createStamp()
    const createCurrent = () =>
      stillHere() && token === requestToken && createdAt === nl2sqlPageGuard.createStamp()
    sessionsNotice.value = ''
    try {
      const info = await createSession(selectedSourceId.value)
      if (!createCurrent()) return null
      return info
    } catch (error) {
      if (!createCurrent()) return null
      if (
        error instanceof Nl2sqlApiError &&
        (error.code === 'timeout' || error.code === 'network_error')
      ) {
        // 创建结果未确认：统一文案，不自动重试，也不再刷新全局列表（doc 10/11）
        sessionsNotice.value = CREATE_PENDING_NOTICE
        return null
      }
      sessionsNotice.value =
        error instanceof Nl2sqlApiError || error instanceof Nl2sqlNotConfiguredError
          ? messageForNl2sql(error)
          : '会话创建失败，请重试。'
      return null
    }
  }

  /** 把 /query/new 替换为会话地址；query 只保留规范化的 home，不带预选 source_id。
   * 导航守卫抛错、异步组件加载 reject 等 Promise 拒绝同样按导航失败处理（doc11 R1-02）；
   * 过期流程的提示写入由调用方的归属检查兜底。 */
  async function replaceToSessionRoute(sessionId: string): Promise<boolean> {
    const query: Record<string, string> = {}
    const home = router.currentRoute.value.query.home
    if (typeof home === 'string') query.home = sessionBackTarget(home)
    let failure: unknown = undefined
    try {
      failure = await router.replace({
        name: 'nl2sql-query-session',
        params: { sessionId },
        query,
      })
    } catch {
      return false
    }
    // replace 以 resolve 送达导航失败：须核对导航结果与实际路由会话 ID（doc 11）
    return (
      failure == null && router.currentRoute.value.params.sessionId === sessionId
    )
  }

  /** 新建页预选变化时放弃“已接管但尚未完成路由交接”的会话状态（doc11 R1-01）。
   * 仅当地址仍在新建页（未对齐到会话）时生效；保留草稿等待新来源校验；
   * 不触碰服务端旧空会话，也不影响已打开的真实历史会话。 */
  function releaseUnalignedTakeover() {
    if (router.currentRoute.value.params.sessionId !== undefined) return
    if (currentSessionId.value === '' && sessionInfo.value === null) return
    requestToken += 1
    creatingSession.value = false
    stopElapsed()
    currentSessionId.value = ''
    sessionInfo.value = null
    currentTurn.value = null
    askState.value = 'idle'
    askNotice.value = ''
  }

  /**
   * 统一发送入口（doc 11）：
   * 无会话 → 创建 → 接管 → replace 对齐 → 提问；
   * 已有会话且地址对齐 → 直接提问；地址未对齐（创建后路由失败重试）→ 只补路由再提问。
   */
  async function sendQuestion(question: string): Promise<void> {
    if (busy.value) return
    if (question.trim() === '' || codepointLength(question) > QUESTION_MAX) return
    if (!canCompose.value) return
    const sessionId = currentSessionId.value
    if (sessionId !== '') {
      if (router.currentRoute.value.params.sessionId === sessionId) {
        await ask(question)
        return
      }
      creatingSession.value = true
      const token = requestToken
      const stamp = nl2sqlPageGuard.createStamp()
      const flowCurrent = () =>
        stillHere() && token === requestToken && stamp === nl2sqlPageGuard.createStamp()
      try {
        const navOk = await replaceToSessionRoute(sessionId)
        if (!navOk) {
          if (flowCurrent()) {
            askNotice.value = '会话已创建，但页面切换失败，尚未提交问题；请再次发送。'
          }
          return
        }
        if (flowCurrent() && canSubmit.value) await ask(question)
      } finally {
        if (flowCurrent()) creatingSession.value = false
      }
      return
    }
    // 首次发送：同步置忙碌后再进入第一个 await，双击/连发不重复创建
    creatingSession.value = true
    const token = requestToken
    const stamp = nl2sqlPageGuard.createStamp()
    const flowCurrent = () =>
      stillHere() && token === requestToken && stamp === nl2sqlPageGuard.createStamp()
    try {
      const info = await createSessionForFirstQuestion()
      if (info === null || !flowCurrent()) return
      // 接管返回的真实会话：不重置、不清草稿、不额外请求详情和历史
      currentSessionId.value = info.session_id
      sessionInfo.value = info
      if (inputBlockedReason.value !== '') return
      const navOk = await replaceToSessionRoute(info.session_id)
      if (!navOk) {
        if (flowCurrent()) {
          askNotice.value = '会话已创建，但页面切换失败，尚未提交问题；请再次发送。'
        }
        return
      }
      if (flowCurrent() && canSubmit.value) await ask(question)
    } finally {
      if (flowCurrent()) creatingSession.value = false
    }
  }

  // ------------------------------------------------------------- 提问

  async function refreshCurrentDetail(sessionId: string, token: number) {
    try {
      const info = await getSession(sessionId)
      if (!stillHere() || token !== requestToken || currentSessionId.value !== sessionId) return
      sessionInfo.value = info
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
    // 通过入口检查、即将真实提交：清空所提交的草稿；只清仍等于本次问题的草稿，
    // 不让迟到回调删除后续输入（doc 11 任务 1）
    if (questionDraft.value === question) questionDraft.value = ''
    elapsedSeconds.value = 0
    stopElapsed()
    elapsedTimer = setInterval(() => {
      elapsedSeconds.value += 1
    }, 1000)
    try {
      const result = await submitQuery(sessionId, question, sourceId)
      if (!stillHere() || token !== requestToken || currentSessionId.value !== sessionId) return
      currentTurn.value = { question, result, error: null, errorMessage: '' }
      askState.value = 'done'
      // 不重读历史：已保存轮次在下次提问/重开会话时转入历史展示；只刷新详情。
      await refreshCurrentDetail(sessionId, token)
    } catch (error) {
      if (!stillHere() || token !== requestToken || currentSessionId.value !== sessionId) return
      const apiError =
        error instanceof Nl2sqlApiError
          ? error
          : new Nl2sqlApiError('internal_error', '请求失败，请稍后重试。')
      if (apiError.code === 'session_busy') {
        // 保留输入，不自动重试（doc 2 §7.2；本约定由 doc 11 落地恢复）：
        // 仅在归属仍有效且草稿为空时恢复本次问题，不覆盖新输入
        askNotice.value = messageForNl2sql(apiError)
        if (questionDraft.value === '') questionDraft.value = question
        currentTurn.value = null
        askState.value = 'idle'
        return
      }
      if (apiError.code === 'session_metadata_invalid') {
        // 立即禁用输入，重读一次详情决定只读或恢复（doc 2 §7.1）
        try {
          const info = await getSession(sessionId)
          if (!stillHere() || token !== requestToken || currentSessionId.value !== sessionId) return
          sessionInfo.value = info
        } catch (retryError) {
          if (!stillHere() || token !== requestToken || currentSessionId.value !== sessionId) return
          if (
            retryError instanceof Nl2sqlApiError &&
            retryError.code === 'session_metadata_invalid'
          ) {
            readonlyHistory.value = true
          }
        }
        if (!stillHere() || token !== requestToken || currentSessionId.value !== sessionId) return
        askNotice.value = messageForNl2sql(apiError)
        currentTurn.value = { question, result: null, error: apiError, errorMessage: '' }
        askState.value = 'error'
        return
      }
      if (SESSION_DEAD_CODES.has(apiError.code)) {
        await refreshCurrentDetail(sessionId, token)
      }
      if (!stillHere() || token !== requestToken || currentSessionId.value !== sessionId) return
      currentTurn.value = {
        question,
        result: (apiError.data as { result?: WebQueryResult } | null)?.result ?? null,
        error: apiError,
        errorMessage: messageForNl2sql(apiError),
      }
      askState.value = 'error'
    } finally {
      if (stillHere() && token === requestToken) stopElapsed()
    }
  }

  // ------------------------------------------------------------- 初始化

  async function initialize() {
    await loadSources()
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
    invalidatePendingCreate,
    sessionsNotice,
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
    resetCurrentSession,
    hasCurrentSession,
    releaseUnalignedTakeover,
    // 提问
    questionDraft,
    creatingSession,
    busy,
    canCompose,
    sendQuestion,
    askState,
    currentTurn,
    elapsedSeconds,
    askNotice,
    // 生命周期
    initialize,
  }
}
