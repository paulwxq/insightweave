<script setup lang="ts">
/**
 * 侧栏历史对话。点击恢复会话，不触发数据库就绪检查。
 */
import type { SessionInfo } from '../api/types'
import { formatRelativeTime } from '../format'

defineProps<{
  sessions: SessionInfo[]
  loading: boolean
  error: string
  hasMore: boolean
  notice: string
  currentSessionId: string
}>()

const emit = defineEmits<{
  open: [sessionId: string]
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
  <section class="history">
    <header>
      <span>历史对话</span>
      <button type="button" @click="emit('refresh')">刷新</button>
    </header>
    <p v-if="notice" class="hint warn">{{ notice }}</p>
    <p v-if="error" class="hint warn">{{ error }}</p>
    <p v-else-if="loading && sessions.length === 0" class="hint">加载中…</p>
    <p v-else-if="sessions.length === 0" class="hint">暂无历史对话</p>
    <ul v-else>
      <li v-for="session in sessions" :key="session.session_id">
        <button
          type="button"
          :class="{ active: session.session_id === currentSessionId }"
          :title="session.title"
          @click="emit('open', session.session_id)"
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path
              d="M3 3.5h10v7.2H6.2L3 12.8V3.5Z"
            />
          </svg>
          <span>
            <strong>{{ session.title }}</strong>
            <small>
              {{ formatRelativeTime(session.last_active_at) }}
              <template v-if="stateText(session)"> · {{ stateText(session) }}</template>
            </small>
          </span>
        </button>
      </li>
    </ul>
    <button v-if="hasMore && !loading" type="button" class="more" @click="emit('loadMore')">
      加载更多
    </button>
  </section>
</template>

<style scoped>
.history {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 0;
  flex: 1;
}

header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: #66788f;
  font-size: 11px;
  padding: 0 2px;
}

header button,
.more {
  border: 0;
  background: transparent;
  color: #2e7cd6;
  font-size: 11px;
  font-weight: 700;
  padding: 0;
}

ul {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 4px;
  overflow: auto;
  min-height: 0;
}

li button {
  width: 100%;
  display: flex;
  align-items: flex-start;
  gap: 8px;
  text-align: left;
  border: 1px solid transparent;
  background: transparent;
  border-radius: 10px;
  padding: 8px 10px;
  color: #66788f;
}

li button:hover {
  background: #f4f8fc;
}

li button.active {
  background: #eef3f9;
  border-color: #d9e3ef;
  color: #17293f;
}

li svg {
  width: 14px;
  height: 14px;
  margin-top: 2px;
  flex-shrink: 0;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linejoin: round;
}

li button.active svg {
  stroke: #2e7cd6;
}

li span {
  min-width: 0;
  display: grid;
  gap: 2px;
}

strong {
  font-size: 12px;
  font-weight: 400;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

li button.active strong {
  font-weight: 700;
}

small {
  color: #66788f;
  font-size: 10px;
}

.hint {
  margin: 0;
  color: #66788f;
  font-size: 12px;
  padding: 6px 2px;
}

.hint.warn {
  color: #9a6b32;
}

.more {
  align-self: center;
}
</style>
