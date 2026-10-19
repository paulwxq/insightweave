<script setup lang="ts">
/**
 * 对话消息：用户气泡在右，回答为结果卡片。
 * 历史轮次若带有展示投影，则回放表格和 SQL；否则只显示问答文本。
 */
import { computed } from 'vue'
import type { TurnEvidenceView, WebTurn } from '../api/types'
import type { CurrentTurn } from '../composables/useChat'
import { formatTurnDivider } from '../format'
import AnswerCard from './AnswerCard.vue'

const props = defineProps<{
  turns: WebTurn[]
  turnsLoading: boolean
  turnsError: string
  hasEarlier: boolean
  currentTurn: CurrentTurn | null
  submitting: boolean
  elapsedSeconds: number
  sessionId: string
}>()

const emit = defineEmits<{ reloadTurns: []; loadEarlier: [] }>()

interface Bubble {
  key: string
  question: string
  answer: string
  savedAt: string | null
  elapsedMs: number | null
  evidence: TurnEvidenceView[]
  clarify: string[]
  unsafe: boolean
  pending: boolean
  errorMessage: string
}

const bubbles = computed<Bubble[]>(() => {
  const items: Bubble[] = props.turns.map((turn, index) => ({
    key: `history-${index}`,
    question: turn.question,
    answer: turn.answer,
    savedAt: turn.saved_at ?? null,
    elapsedMs: turn.elapsed_ms ?? null,
    evidence: turn.evidence ?? [],
    clarify: [],
    unsafe: false,
    pending: false,
    errorMessage: '',
  }))
  const current = props.currentTurn
  if (!current) return items
  const result = current.result
  const unsafe =
    result?.error_code === 'knowledge_changed' || result?.error_code === 'dependency_unavailable'
  items.push({
    key: 'current',
    question: current.question,
    answer: result?.answer ?? '',
    savedAt: new Date().toISOString(),
    elapsedMs: result?.stats.elapsed_ms ?? null,
    evidence:
      !result || unsafe
        ? []
        : result.evidence.map((item) => ({
            sql: item.sql,
            result_ref: item.result_ref,
            row_count: item.row_count,
            source_truncated: item.source_truncated,
          })),
    clarify: result?.status === 'needs_clarification' ? result.open_questions : [],
    unsafe,
    pending: props.submitting,
    errorMessage: current.errorMessage,
  })
  return items
})

const groups = computed(() => {
  const grouped: { label: string; items: Bubble[] }[] = []
  for (const item of bubbles.value) {
    const label = formatTurnDivider(item.savedAt)
    const last = grouped[grouped.length - 1]
    if (!last || last.label !== label) grouped.push({ label, items: [item] })
    else last.items.push(item)
  }
  return grouped
})
</script>

<template>
  <div class="messages">
    <div v-if="hasEarlier" class="earlier">
      <button type="button" :disabled="turnsLoading" @click="emit('loadEarlier')">
        {{ turnsLoading ? '读取中…' : '加载更早问答' }}
      </button>
    </div>

    <template v-for="group in groups" :key="group.label">
      <div class="day">
        <i></i>
        <span>{{ group.label }}</span>
        <i></i>
      </div>
      <div v-for="item in group.items" :key="item.key" class="turn">
        <div class="user-row">
          <div class="user-bubble">{{ item.question }}</div>
        </div>

        <div v-if="item.pending" class="ai-row">
          <div class="avatar" aria-hidden="true"></div>
          <article class="card pending">正在执行，已等待 {{ elapsedSeconds }} 秒。请勿关闭页面或重复提交。</article>
        </div>
        <div v-else-if="item.errorMessage && !item.answer" class="ai-row">
          <div class="avatar" aria-hidden="true"></div>
          <article class="card error">{{ item.errorMessage }}</article>
        </div>
        <AnswerCard
          v-else-if="item.answer || item.evidence.length > 0"
          :session-id="sessionId"
          :answer="item.answer"
          :elapsed-ms="item.elapsedMs"
          :evidence="item.evidence"
          :clarify="item.clarify"
          :unsafe="item.unsafe"
        />
      </div>
    </template>

    <div v-if="turnsLoading && bubbles.length === 0" class="empty">读取历史…</div>
    <div v-else-if="turnsError" class="empty error">
      <p>{{ turnsError }}</p>
      <button type="button" @click="emit('reloadTurns')">重试</button>
    </div>
    <div v-else-if="bubbles.length === 0" class="empty">
      {{
        sessionId
          ? '本会话还没有问答，直接在下方描述你的问题。'
          : '选择数据库并完成检查后，用自然语言开始提问。'
      }}
    </div>
  </div>
</template>

<style scoped>
.messages {
  flex: 1;
  min-height: 0;
  overflow: auto;
  background: #f2f6fb;
  padding: 16px 26px 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.earlier,
.empty {
  display: flex;
  justify-content: center;
}

.earlier button,
.empty button {
  border: 1px solid #d9e3ef;
  background: #fff;
  color: #2e7cd6;
  border-radius: 999px;
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 700;
}

.day {
  display: flex;
  align-items: center;
  gap: 12px;
  color: #66788f;
  font-size: 11px;
}

.day i {
  flex: 1;
  height: 1px;
  background: #d9e3ef;
}

.turn {
  display: grid;
  gap: 12px;
}

.user-row {
  display: flex;
  justify-content: flex-end;
}

.user-bubble {
  max-width: min(520px, 80%);
  background: #2e7cd6;
  color: #fff;
  border-radius: 16px 16px 4px 16px;
  padding: 12px 16px;
  font-size: 13px;
  line-height: 21px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.ai-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.avatar {
  width: 32px;
  height: 32px;
  border-radius: 16px;
  background: #e3eefb;
  flex-shrink: 0;
}

.card {
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 14px;
  padding: 14px 16px;
  font-size: 13px;
  line-height: 21px;
  color: #41556d;
}

.card.pending {
  color: #66788f;
}

.card.error,
.empty.error {
  color: #a54756;
}

.empty {
  color: #66788f;
  font-size: 13px;
  padding: 48px 12px;
  text-align: center;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}

.empty p {
  margin: 0;
}
</style>
