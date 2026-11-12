<script setup lang="ts">
import type { SessionInfo } from '../api/types'
import { formatNl2sqlTime } from '../format'

defineProps<{
  sessions: SessionInfo[]
  loading: boolean
  error: string
  hasMore: boolean
  currentSessionId?: string
  emptyText: string
  databaseLabel: (sourceId: string) => string
  inactive: (sourceId: string) => boolean
  canCreate: (sourceId: string) => boolean
  actionsDisabled?: boolean
}>()

const emit = defineEmits<{
  open: [sessionId: string]
  create: [sourceId: string]
  loadMore: []
  refresh: []
}>()

function stateText(session: SessionInfo): string {
  if (session.state === 'invalidated') return '已失效'
  if (!session.continuable) return '待确认'
  return ''
}
</script>

<template>
  <div class="history">
    <div v-if="error && sessions.length > 0" class="history-note">
      <span>{{ error }}</span>
      <button type="button" class="button button-outline small-button" @click="emit('loadMore')">重试</button>
    </div>

    <div v-if="loading && sessions.length === 0" class="state-block">正在加载对话…</div>
    <div v-else-if="error && sessions.length === 0" class="state-block error-state">
      <p>{{ error }}</p>
      <button type="button" class="button button-outline small-button" @click="emit('refresh')">重试</button>
    </div>
    <div v-else-if="sessions.length === 0" class="state-block">{{ emptyText }}</div>

    <ul v-else class="history-list">
      <li v-for="session in sessions" :key="session.session_id">
        <article class="history-row" :class="{ current: session.session_id === currentSessionId }">
          <button
            type="button"
            class="history-open"
            :disabled="actionsDisabled"
            @click="emit('open', session.session_id)"
          >
            <strong>{{ session.title }}</strong>
            <span>
              {{ databaseLabel(session.source_id) }}
              <em v-if="inactive(session.source_id)">已停用</em>
              <em v-if="stateText(session)" class="state">{{ stateText(session) }}</em>
            </span>
          </button>
          <time>{{ formatNl2sqlTime(session.last_active_at) }}</time>
          <button
            v-if="canCreate(session.source_id)"
            type="button"
            class="button button-outline small-button"
            :disabled="actionsDisabled"
            @click="emit('create', session.source_id)"
          >
            在此数据库新建
          </button>
        </article>
      </li>
    </ul>

    <div v-if="hasMore" class="history-more">
      <button
        type="button"
        class="button button-outline small-button"
        :disabled="loading"
        @click="emit('loadMore')"
      >
        {{ loading ? '正在加载…' : '加载更多' }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.history {
  min-width: 0;
}

.history-note,
.history-more {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  color: #9a6b32;
  font-size: 13px;
}

.history-more {
  justify-content: center;
  margin: 16px 0 0;
}

.history-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 10px;
}

.history-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 16px;
  padding: 14px 16px;
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 14px;
}

.history-row.current {
  border-color: #2e7cd6;
  background: #eef3f9;
}

.history-open {
  min-width: 0;
  padding: 0;
  border: 0;
  background: transparent;
  text-align: left;
  color: inherit;
}

.history-open strong,
.history-open span {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-open strong {
  color: #17293f;
  font-size: 15px;
}

.history-open span,
time {
  margin-top: 4px;
  color: #66788f;
  font-size: 12px;
}

.history-open em {
  margin-left: 8px;
  font-style: normal;
  color: #9a6b32;
}

.history-open em.state {
  color: #8a5a44;
}

time {
  margin-top: 0;
  white-space: nowrap;
}

.history-open:disabled {
  cursor: not-allowed;
}

@media (max-width: 800px) {
  .history-row {
    grid-template-columns: minmax(0, 1fr);
    align-items: start;
  }
}
</style>
