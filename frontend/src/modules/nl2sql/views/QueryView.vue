<script setup lang="ts">
/**
 * 自然语言查询页。
 *
 * /query：顶部选择数据库并检查就绪，通过后创建会话。
 * /query/:sessionId：直接打开会话，不再检查数据库。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { nl2sqlBaseUrl } from '../api/client'
import type { TurnEvidenceView, WebQueryResult } from '../api/types'
import { useNl2sqlChat } from '../composables/useChat'
import { formatClock } from '../format'
import MessageList from '../components/MessageList.vue'
import QuestionInput from '../components/QuestionInput.vue'
import SourceSelector from '../components/SourceSelector.vue'

const route = useRoute()
const realRouter = useRouter()
const chat = useNl2sqlChat(realRouter)

const configError = ref('')
const creating = ref(false)
const draftEpoch = ref(0)

const newConversationMode = computed(() => chat.currentSessionId.value === '')

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
  if (newConversationMode.value) return '请先在顶部选择数据库，检查通过后再开始查询。'
  return chat.inputBlockedReason.value
})

watch(
  () => chat.currentSessionId.value,
  () => {
    draftEpoch.value += 1
  },
)

watch(
  () => route.params.sessionId,
  (value) => {
    const sessionId = typeof value === 'string' ? value : ''
    if (sessionId === '') {
      chat.resetCurrentSession()
      chat.resetReadiness()
    } else {
      void chat.openSession(sessionId)
    }
  },
  { immediate: true },
)

onMounted(async () => {
  try {
    nl2sqlBaseUrl()
  } catch {
    configError.value = '未配置 VITE_NL2SQL_API_BASE_URL，NL2SQL 查询不可用；请配置后重新构建。'
    return
  }
  await chat.initialize()
  const preset = route.query.source_id
  if (typeof preset === 'string' && preset !== '' && !route.params.sessionId) {
    void chat.checkReadiness(preset)
  }
})

async function onCreateSession() {
  creating.value = true
  try {
    await chat.createNewSession()
  } finally {
    creating.value = false
  }
}

function onAsk(question: string) {
  void chat.ask(question)
}

async function refreshCurrent() {
  const sessionId = chat.currentSessionId.value
  if (sessionId === '') return
  await chat.openSession(sessionId)
  await chat.refreshSessions()
}
</script>

<template>
  <div class="nl2sql-page">
    <div v-if="configError" class="state-block error-state">
      <h3>查询服务未配置</h3>
      <p>{{ configError }}</p>
    </div>

    <section v-else class="window">
      <header class="window-head">
        <div class="head-mark">
          <span class="mark-icon" aria-hidden="true">
            <svg viewBox="0 0 16 16"><path d="M3 4h4l1.2 2H13v6H3V4Zm6 4.2h3" /></svg>
          </span>
          <strong>自然语言查询</strong>
        </div>
        <div class="head-title">
          <h1>{{ headerTitle }}</h1>
          <p>{{ headerMeta }}</p>
        </div>
        <div class="head-actions">
          <button type="button" class="icon-btn" disabled title="重命名尚未开放" aria-label="重命名">
            <svg viewBox="0 0 16 16"><path d="M9.5 3.5 12.5 6.5 6 13H3v-3l6.5-6.5Z" /></svg>
          </button>
          <button type="button" class="icon-btn" aria-label="刷新对话" title="刷新对话" @click="refreshCurrent">
            <svg viewBox="0 0 16 16"><path d="M3 8a5 5 0 0 1 8.5-3.5L13 6M13 3v3h-3M13 8a5 5 0 0 1-8.5 3.5L3 10M3 13v-3h3" /></svg>
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
        :creating="creating"
        :notice="chat.sessionsNotice.value"
        @select="chat.checkReadiness"
        @create="onCreateSession"
      />

      <p v-if="chat.sessionLoadError.value" class="banner">{{ chat.sessionLoadError.value }}</p>
      <p v-else-if="chat.readonlyHistory.value" class="banner">
        只读历史模式：会话展示信息不完整，可刷新；若无法恢复请新建会话。
      </p>
      <p v-if="chat.askNotice.value" class="banner">{{ chat.askNotice.value }}</p>

      <MessageList
        :turns="chat.turns.value"
        :turns-loading="chat.turnsLoading.value"
        :turns-error="chat.turnsError.value"
        :has-earlier="chat.turnsHasEarlier.value"
        :current-turn="chat.currentTurn.value"
        :submitting="chat.askState.value === 'submitting'"
        :elapsed-seconds="chat.elapsedSeconds.value"
        :session-id="chat.currentSessionId.value"
        @reload-turns="chat.openSession(chat.currentSessionId.value)"
        @load-earlier="chat.loadEarlierTurns"
      />

      <QuestionInput
        :disabled="!chat.canSubmit.value"
        :blocked-reason="inputBlockedReason"
        :submitting="chat.askState.value === 'submitting'"
        :elapsed-seconds="chat.elapsedSeconds.value"
        :draft-epoch="draftEpoch"
        :source-label="composerSourceLabel"
        @submit="onAsk"
      />
    </section>
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
  min-height: calc(100vh - 56px);
  display: flex;
  flex-direction: column;
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 16px;
  overflow: hidden;
}

.window-head {
  display: grid;
  grid-template-columns: 160px 1fr 160px;
  align-items: center;
  gap: 8px;
  min-height: 64px;
  padding: 10px 18px;
  border-bottom: 1px solid #d9e3ef;
  background: #fff;
}

.head-mark {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #66788f;
  font-size: 13px;
}

.mark-icon {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: #e3eefb;
  display: grid;
  place-items: center;
}

.mark-icon svg,
.icon-btn svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: #2e7cd6;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.head-title {
  text-align: center;
  min-width: 0;
}

.head-title h1 {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
  color: #17293f;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.head-title p {
  margin: 3px 0 0;
  color: #66788f;
  font-size: 10px;
}

.head-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.icon-btn {
  width: 34px;
  height: 34px;
  border-radius: 9px;
  border: 1px solid #d9e3ef;
  background: #fff;
  display: grid;
  place-items: center;
}

.icon-btn svg {
  stroke: #66788f;
}

.icon-btn:disabled {
  opacity: 0.7;
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
  .window-head {
    grid-template-columns: 1fr auto;
  }

  .head-mark {
    display: none;
  }
}
</style>
