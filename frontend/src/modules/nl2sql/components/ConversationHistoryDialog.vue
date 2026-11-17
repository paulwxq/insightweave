<script setup lang="ts">
/**
 * 查询页历史对话框。筛选只留在对话框内，每次打开都回到全部数据库和空搜索。
 */
import { nextTick, onUnmounted, ref, watch } from 'vue'
import { nl2sqlBaseUrl } from '../api/client'
import ConversationList from './ConversationList.vue'
import { sessionKeyword } from '../format'
import { createSessionList } from '../sessionList'
import { useSourceCatalog } from '../sourceCatalog'

const props = defineProps<{
  open: boolean
  currentSessionId: string
  actionsDisabled: boolean
}>()

const emit = defineEmits<{
  close: []
  openSession: [sessionId: string]
  create: [sourceId: string]
}>()

const panel = ref<HTMLElement | null>(null)
const list = createSessionList()
const { sessions, loading, error, hasMore, totalAll, loadFirst, loadMore } = list
const catalog = useSourceCatalog()
const {
  error: sourcesError,
  load: reloadSources,
  choices,
  databaseLabel,
  inactive,
  canCreate,
} = catalog
const configured = ref(true)
const configMessage = ref('')
const sourceId = ref('')
const draft = ref('')
const appliedKeyword = ref('')
const searchError = ref('')

function reload() {
  if (!configured.value) return
  void list.loadFirst({
    sourceId: sourceId.value || undefined,
    q: appliedKeyword.value || undefined,
  })
}

function submitSearch() {
  const result = sessionKeyword(draft.value)
  searchError.value = result.error
  if (result.error) return
  appliedKeyword.value = result.keyword
  reload()
}

function resetFilters() {
  sourceId.value = ''
  draft.value = ''
  appliedKeyword.value = ''
  searchError.value = ''
  reload()
}

function onKeydown(event: KeyboardEvent) {
  if (!props.open) return
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
    return
  }
  if (event.key !== 'Tab' || !panel.value) return
  const items = [
    ...panel.value.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    ),
  ].filter((item) => !item.hasAttribute('disabled'))
  if (items.length === 0) return
  const first = items[0]
  const last = items[items.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

watch(
  () => props.open,
  (open) => {
    if (!open) return
    sourceId.value = ''
    draft.value = ''
    appliedKeyword.value = ''
    searchError.value = ''
    try {
      nl2sqlBaseUrl()
      configured.value = true
    } catch (error) {
      configured.value = false
      configMessage.value = error instanceof Error ? error.message : '查询服务未配置'
    }
    if (configured.value) {
      void catalog.load()
      reload()
    }
    void nextTick(() => panel.value?.focus())
  },
)

window.addEventListener('keydown', onKeydown)
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div v-if="open" class="dialog-backdrop history-backdrop" @click.self="emit('close')">
    <div
      ref="panel"
      class="dialog-panel history-panel"
      role="dialog"
      aria-modal="true"
      aria-labelledby="history-title"
      tabindex="-1"
    >
      <header class="history-head">
        <div>
          <span class="ds-kicker">NL2SQL</span>
          <h2 id="history-title">查询历史</h2>
        </div>
        <button type="button" class="button button-outline small-button" @click="emit('close')">关闭</button>
      </header>

      <div v-if="!configured" class="state-block error-state">
        <h3>查询服务未配置</h3>
        <p>{{ configMessage }}</p>
      </div>

      <template v-else>
        <form class="history-filters" @submit.prevent="submitSearch">
          <label>
            数据库
            <select v-model="sourceId" @change="reload">
              <option value="">全部数据库</option>
              <option v-for="item in choices(sourceId)" :key="item.value" :value="item.value">
                {{ item.label }}
              </option>
            </select>
          </label>
          <label class="keyword">
            标题
            <input v-model="draft" type="search" placeholder="搜索对话标题" />
          </label>
          <button type="submit" class="button button-primary small-button">搜索</button>
          <button type="button" class="button button-outline small-button" @click="resetFilters">重置</button>
        </form>
        <p v-if="searchError" class="filter-error">{{ searchError }}</p>
        <p v-if="sourcesError" class="filter-error">
          {{ sourcesError }}
          <button type="button" class="button button-outline small-button" @click="reloadSources()">重试</button>
        </p>
        <ConversationList
          :sessions="sessions"
          :loading="loading"
          :error="error"
          :has-more="hasMore"
          :current-session-id="currentSessionId"
          :empty-text="totalAll === 0 ? '暂无对话' : '没有符合条件的对话'"
          :database-label="databaseLabel"
          :inactive="inactive"
          :can-create="canCreate"
          :actions-disabled="actionsDisabled"
          @open="emit('openSession', $event)"
          @create="emit('create', $event)"
          @load-more="loadMore()"
          @refresh="reload"
        />
      </template>
    </div>
  </div>
</template>

<style scoped>
.history-panel {
  width: min(920px, 100%);
  max-height: min(760px, calc(100vh - 48px));
  overflow: auto;
}

.history-head,
.history-filters {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 12px;
}

.history-head {
  align-items: center;
  margin-bottom: 16px;
}

.history-head h2 {
  margin: 6px 0 0;
  color: #17293f;
  font-size: 22px;
}

.history-filters {
  flex-wrap: wrap;
  margin-bottom: 14px;
}

.history-filters label {
  display: grid;
  gap: 6px;
  color: #66788f;
  font-size: 12px;
  font-weight: 700;
}

.history-filters select,
.history-filters input {
  min-width: 180px;
  height: 40px;
  padding: 0 12px;
  border: 1px solid #d9e3ef;
  border-radius: 12px;
  background: #fff;
  color: #17293f;
}

.keyword {
  flex: 1;
}

.keyword input {
  width: 100%;
}

.filter-error {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 0 12px;
  color: #9a6b32;
  font-size: 13px;
}

@media (max-width: 800px) {
  .history-backdrop {
    padding: 0;
  }

  .history-panel {
    width: 100%;
    height: 100%;
    max-height: none;
    border-radius: 0;
  }
}
</style>
