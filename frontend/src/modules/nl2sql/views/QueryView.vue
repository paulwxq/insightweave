<script setup lang="ts">
/**
 * 自然语言查询页。
 *
 * /query/new：顶部选择数据库并检查就绪；就绪通过即可输入，首次发送时创建会话
 * 并把地址替换为会话页（doc 11）。地址中的 source_id 只在此页预选。
 * /query/:sessionId：直接打开会话，由服务端解析绑定，不使用地址中的 source_id。
 * 两个页面都有「返回」。地址中的 home 只记录问数首页，不预选数据库。
 */
import { computed, inject, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { nl2sqlBaseUrl } from '../api/client'
import type { TurnEvidenceView, WebQueryResult } from '../api/types'
import ConversationHistoryDialog from '../components/ConversationHistoryDialog.vue'
import { useNl2sqlChat } from '../composables/useChat'
import { formatClock } from '../format'
import { sessionBackTarget } from '../queryHomeLink'
import { bindQueryBusy, ownsQueryBusy, queryBusyKey } from '../queryBusy'
import MessageList from '../components/MessageList.vue'
import QuestionInput from '../components/QuestionInput.vue'
import SourceSelector from '../components/SourceSelector.vue'

const route = useRoute()
const realRouter = useRouter()
const chat = useNl2sqlChat(realRouter)

const configError = ref('')
const apiReady = ref(false)
const historyOpen = ref(false)
const historyButton = ref<HTMLButtonElement | null>(null)
const queryBusy = inject(queryBusyKey, null)
const busyBinding = queryBusy ? bindQueryBusy(queryBusy) : null
/** 页面统一忙碌（doc 11）：创建中或查询执行中，入口与共享标记都读它。 */
const busy = computed(() => chat.busy.value)

watch(
  busy,
  (value) => {
    if (!queryBusy || !busyBinding || !ownsQueryBusy(busyBinding.token)) return
    queryBusy.value = value
  },
  { immediate: true },
)
onUnmounted(() => busyBinding?.release())

const newConversationMode = computed(() => chat.currentSessionId.value === '')
const onSessionPage = computed(() => route.name === 'nl2sql-query-session')
const backTarget = computed(() => sessionBackTarget(route.query.home))
const newQueryTo = computed(() => {
  const query: Record<string, string> = {}
  if (typeof route.query.home === 'string') query.home = sessionBackTarget(route.query.home)
  return { path: '/query/new', query }
})

const headerTitle = computed(() => {
  if (newConversationMode.value) return '新建查询'
  return chat.sessionInfo.value?.title || '历史对话'
})

function countEvidenceSql(items: TurnEvidenceView[]): number {
  return items.filter((item) => item.sql && item.sql.trim() !== '').length
}

function countResultSql(result: WebQueryResult | null): number {
  if (!result) return 0
  if (result.error_code === 'knowledge_changed' || result.error_code === 'dependency_unavailable') {
    return 0
  }
  return result.evidence.filter((item) => item.sql && item.sql.trim() !== '').length
}

const sqlCount = computed(() => {
  const history = chat.turns.value.reduce(
    (total, turn) => total + countEvidenceSql(turn.evidence ?? []),
    0,
  )
  return history + countResultSql(chat.currentTurn.value?.result ?? null)
})

const headerMeta = computed(() => {
  if (newConversationMode.value) return '选择数据库并完成检查后开始提问'
  const clock = formatClock(chat.sessionInfo.value?.last_active_at)
  const updated = clock ? `更新于 ${clock}` : '尚未更新'
  return `${updated} · 已生成 ${sqlCount.value} 条 SQL`
})

const composerSourceLabel = computed(() => {
  const sourceId = newConversationMode.value
    ? chat.selectedSourceId.value
    : chat.sessionInfo.value?.source_id
  if (!sourceId) return ''
  const source = chat.sources.value.find((item) => item.source_id === sourceId)
  if (source) return source.display_name
  return newConversationMode.value ? '' : '已绑定数据库'
})

const inputBlockedReason = computed(() => {
  if (newConversationMode.value) {
    // doc 11：按未选库、检查中、未就绪/失败分别说明；创建等待由输入组件提示
    if (chat.selectedSourceId.value === '') return '请先在顶部选择数据库，完成后即可输入问题。'
    if (chat.readinessState.value === 'checking') return '正在检查数据源…'
    if (chat.readinessState.value === 'blocked') return '数据源暂未就绪，请查看上方检查结果。'
    if (chat.readinessState.value === 'error') {
      return chat.readinessError.value || '就绪检查失败，请稍后重试。'
    }
    return ''
  }
  return chat.inputBlockedReason.value
})

watch(
  () => route.params.sessionId,
  (value) => {
    const sessionId = typeof value === 'string' ? value : ''
    if (sessionId === '') {
      chat.resetCurrentSession()
      chat.resetReadiness()
    } else if (!chat.hasCurrentSession(sessionId)) {
      // 首次创建后的接管跳过重置；直接打开/刷新/切换会话仍加载详情与历史（doc 11）
      void chat.openSession(sessionId)
    }
  },
  { immediate: true },
)

const presetSource = computed(() =>
  route.name === 'nl2sql-query' && typeof route.query.source_id === 'string'
    ? route.query.source_id
    : '',
)

watch(
  () => [apiReady.value, route.name, presetSource.value] as const,
  ([ready, name, sourceId], previous) => {
    if (!ready || name !== 'nl2sql-query') return
    const previousSource = previous?.[0] && previous[1] === 'nl2sql-query' ? previous[2] : undefined
    if (previous && sourceId === previousSource) return
    if (previous?.[1] === 'nl2sql-query' && sourceId !== previous[2]) {
      chat.invalidatePendingCreate()
      // 预选变化即放弃“已接管但尚未完成路由交接”的会话，保留草稿等待新校验（doc11 R1-01）
      chat.releaseUnalignedTakeover()
    }
    if (sourceId === '') {
      chat.resetReadiness()
      return
    }
    void chat.checkReadiness(sourceId)
  },
)

onMounted(async () => {
  try {
    nl2sqlBaseUrl()
  } catch {
    configError.value = '未配置 VITE_NL2SQL_API_BASE_URL，NL2SQL 查询不可用；请配置后重新构建。'
    return
  }
  apiReady.value = true
  await chat.initialize()
})

function closeHistory() {
  historyOpen.value = false
  void nextTick(() => historyButton.value?.focus())
}

function onAsk(question: string) {
  void chat.sendQuestion(question)
}

function refreshCurrent() {
  if (busy.value) return
  const sessionId = chat.currentSessionId.value
  if (sessionId === '') return
  void chat.openSession(sessionId)
}

function guardNewQuery(event: MouseEvent) {
  if (busy.value) event.preventDefault()
}

function openHistorySession(sessionId: string) {
  if (busy.value) return
  historyOpen.value = false
  const query: Record<string, string> = {}
  if (onSessionPage.value && typeof route.query.home === 'string') {
    query.home = sessionBackTarget(route.query.home)
  }
  void realRouter.push({ name: 'nl2sql-query-session', params: { sessionId }, query })
}

function createFromHistory(sourceId: string) {
  if (busy.value) return
  historyOpen.value = false
  const query: Record<string, string> = { source_id: sourceId }
  if (typeof route.query.home === 'string') query.home = sessionBackTarget(route.query.home)
  void realRouter.push({ path: '/query/new', query })
}

const copyNote = ref('')
let copyTimer = 0

const showNewGuide = computed(
  () =>
    newConversationMode.value &&
    chat.turns.value.length === 0 &&
    chat.currentTurn.value === null &&
    !chat.turnsLoading.value &&
    chat.turnsError.value === '',
)

/** 空状态步骤：未选库停在 1，检查中或未通过停在 2，就绪后停在 3。 */
const guideStep = computed(() => {
  if (chat.selectedSourceId.value === '') return 1
  if (chat.readinessState.value === 'ready') return 3
  return 2
})

async function copyPageLink() {
  window.clearTimeout(copyTimer)
  try {
    await navigator.clipboard.writeText(window.location.href)
    copyNote.value = '链接已复制'
  } catch {
    copyNote.value = '复制失败'
  }
  copyTimer = window.setTimeout(() => {
    copyNote.value = ''
  }, 1600)
}

onUnmounted(() => window.clearTimeout(copyTimer))
</script>

<template>
  <div class="nl2sql-page">
    <div v-if="configError" class="state-block error-state">
      <h3>查询服务未配置</h3>
      <p>{{ configError }}</p>
    </div>

    <section v-else class="window">
      <header class="window-head">
        <div class="head-side">
          <div class="head-mark">
            <span class="mark-icon" aria-hidden="true">
              <svg viewBox="0 0 16 16"><path d="M3 3.5h7.2a1.3 1.3 0 0 1 1.3 1.3V9L9.2 7.5H3A1.3 1.3 0 0 1 1.7 6.2V4.8A1.3 1.3 0 0 1 3 3.5Z" /><path d="M6.2 8.2h4.6L13.5 10V6.8" /></svg>
            </span>
            自然语言查询
          </div>
        </div>
        <div class="head-title">
          <h1 :class="{ 'is-session': !newConversationMode }">{{ headerTitle }}</h1>
          <p>{{ headerMeta }}</p>
        </div>
        <div class="head-actions">
          <RouterLink class="head-btn query-back" :to="backTarget">
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M12.5 8H3.5" /><path d="m7 4.5-3.5 3.5L7 11.5" /></svg>
            返回
          </RouterLink>
          <RouterLink
            :to="newQueryTo"
            class="head-btn head-btn-primary"
            :aria-disabled="busy || undefined"
            @click="guardNewQuery"
          >
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3.5v9M3.5 8h9" /></svg>
            新建查询
          </RouterLink>
          <button
            ref="historyButton"
            type="button"
            class="head-btn"
            :disabled="busy"
            @click="historyOpen = true"
          >
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 8a5.5 5.5 0 1 0 1.4-3.7" /><path d="M2.2 2.8v2.6h2.6" /><path d="M8 5.2V8l1.8 1.2" /></svg>
            查询历史
          </button>
          <button
            type="button"
            class="icon-btn"
            :aria-label="copyNote || '复制链接'"
            :title="copyNote || '复制链接'"
            @click="copyPageLink"
          >
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6.2 9.2a3 3 0 0 0 4.2.2l1.6-1.6a3 3 0 0 0-4.2-4.2L6.8 4.6" /><path d="M9.8 6.8a3 3 0 0 0-4.2-.2L4 8.2a3 3 0 0 0 4.2 4.2l1-1" /></svg>
          </button>
          <button
            type="button"
            class="icon-btn"
            aria-label="刷新对话"
            title="刷新对话"
            :disabled="busy || newConversationMode"
            @click="refreshCurrent"
          >
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M13.2 8A5.2 5.2 0 1 1 11 3.6" /><path d="M13.4 2.8v3.1H10.3" /></svg>
          </button>
        </div>
      </header>

      <SourceSelector
        v-if="newConversationMode"
        :sources="chat.sources.value"
        :loading="chat.sourcesLoading.value"
        :error="chat.sourcesError.value"
        :selected-source-id="chat.selectedSourceId.value"
        :readiness-state="chat.readinessState.value"
        :readiness-data="chat.readinessData.value"
        :readiness-error="chat.readinessError.value"
        :busy="busy"
        :notice="chat.sessionsNotice.value"
        @select="chat.checkReadiness"
      />

      <p v-if="chat.sessionLoadError.value" class="banner">{{ chat.sessionLoadError.value }}</p>
      <p v-else-if="chat.readonlyHistory.value" class="banner">
        只读历史模式：会话展示信息不完整，可刷新；若无法恢复请新建会话。
      </p>
      <p v-if="chat.askNotice.value" class="banner">{{ chat.askNotice.value }}</p>

      <div v-if="showNewGuide" class="new-guide">
        <div class="guide-icon" aria-hidden="true">
          <svg viewBox="0 0 32 32"><path d="M8 8.5h12.5a3 3 0 0 1 3 3V18l-3.2-2.2H8a3 3 0 0 1-3-3v-1.3a3 3 0 0 1 3-3Z" /><circle cx="12" cy="13.2" r="1" /><circle cx="16" cy="13.2" r="1" /><circle cx="20" cy="13.2" r="1" /></svg>
        </div>
        <h2>选择数据库，开始智能查询</h2>
        <p>选择数据库并完成检查后，用自然语言开始提问。</p>
        <div class="guide-steps">
          <span :class="{ current: guideStep === 1 }"><i>1</i>选择数据库</span>
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
          <span :class="{ current: guideStep === 2 }"><i>2</i>自动完成检查</span>
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
          <span :class="{ current: guideStep === 3 }"><i>3</i>输入自然语言问题</span>
        </div>
      </div>
      <MessageList
        v-else
        :turns="chat.turns.value"
        :turns-loading="chat.turnsLoading.value"
        :turns-error="chat.turnsError.value"
        :has-earlier="chat.turnsHasEarlier.value"
        :current-turn="chat.currentTurn.value"
        :submitting="chat.askState.value === 'submitting'"
        :elapsed-seconds="chat.elapsedSeconds.value"
        :session-id="chat.currentSessionId.value"
        @reload-turns="refreshCurrent"
        @load-earlier="chat.loadEarlierTurns"
      />

      <QuestionInput
        v-model="chat.questionDraft.value"
        :disabled="!chat.canCompose.value"
        :blocked-reason="inputBlockedReason"
        :submitting="chat.askState.value === 'submitting'"
        :creating="chat.creatingSession.value"
        :elapsed-seconds="chat.elapsedSeconds.value"
        :source-label="composerSourceLabel"
        @submit="onAsk"
      />
    </section>
    <ConversationHistoryDialog
      :open="historyOpen"
      :current-session-id="chat.currentSessionId.value"
      :actions-disabled="busy"
      @close="closeHistory"
      @open-session="openHistorySession"
      @create="createFromHistory"
    />
  </div>
</template>

<style scoped>
.nl2sql-page {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
}

.window {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 20px;
  box-shadow: 0 6px 20px rgb(23 41 63 / 5%);
  overflow: hidden;
}

.window-head {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 22px 26px;
  border-bottom: 1px solid #d9e3ef;
  background: #fff;
}

.head-side,
.head-actions {
  flex: 1;
  min-width: 0;
}

.head-mark {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 9px 12px;
  border-radius: 10px;
  background: #e3eefb;
  color: #2e7cd6;
  font-size: 13px;
  font-weight: 700;
  line-height: 1;
}

.mark-icon {
  width: 16px;
  height: 16px;
  display: grid;
  place-items: center;
}

.mark-icon svg,
.head-btn svg,
.icon-btn svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.mark-icon svg {
  width: 16px;
  height: 16px;
}

.head-title {
  text-align: center;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.head-title h1 {
  margin: 0;
  font-size: 22px;
  font-weight: 700;
  color: #17293f;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.head-title h1.is-session {
  max-width: 280px;
  font-size: 16px;
}

.head-title p {
  margin: 0;
  color: #66788f;
  font-size: 11px;
}

.head-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
}

.head-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  height: 34px;
  padding: 0 11px;
  border: 1px solid #d9e3ef;
  border-radius: 9px;
  background: #fff;
  color: #17293f;
  font-size: 12px;
  font-weight: 400;
  line-height: 1;
}

.head-btn svg {
  color: #66788f;
}

.head-btn-primary {
  background: #2e7cd6;
  border-color: #2e7cd6;
  color: #fff;
  font-weight: 700;
}

.head-btn-primary svg {
  color: #fff;
}

.head-btn[aria-disabled='true'],
.head-btn:disabled {
  opacity: 0.55;
}

.icon-btn {
  width: 34px;
  height: 34px;
  border-radius: 9px;
  border: 1px solid #d9e3ef;
  background: #fff;
  color: #66788f;
  display: grid;
  place-items: center;
  flex-shrink: 0;
}

.icon-btn:disabled {
  opacity: 0.55;
}

.new-guide {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 18px;
  padding: 40px 24px;
  background: #fcfdfe;
  text-align: center;
}

.guide-icon {
  width: 68px;
  height: 68px;
  border-radius: 20px;
  background: #e3eefb;
  color: #2e7cd6;
  display: grid;
  place-items: center;
}

.guide-icon svg {
  width: 30px;
  height: 30px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.guide-icon circle {
  fill: currentColor;
  stroke: none;
}

.new-guide h2 {
  margin: 0;
  color: #17293f;
  font-size: 20px;
  font-weight: 700;
}

.new-guide p {
  margin: 0;
  color: #66788f;
  font-size: 13px;
}

.guide-steps {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 10px;
}

.guide-steps > span {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: #66788f;
  font-size: 12px;
}

.guide-steps i {
  width: 25px;
  height: 25px;
  border-radius: 13px;
  border: 1px solid #d9e3ef;
  background: #fff;
  color: #66788f;
  display: grid;
  place-items: center;
  font-size: 11px;
  font-style: normal;
  font-weight: 700;
}

.guide-steps .current i {
  background: #e3eefb;
  border-color: #c9def3;
  color: #2e7cd6;
}

.guide-steps > svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: #aab8c8;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.banner {
  margin: 0;
  padding: 8px 22px;
  background: #fff7ef;
  color: #9a6b32;
  font-size: 12px;
  border-bottom: 1px solid #f0d9bd;
}

@media (max-width: 800px) {
  .head-mark {
    display: none;
  }

  .head-title h1,
  .head-title h1.is-session {
    max-width: 160px;
    font-size: 16px;
  }
}
</style>
