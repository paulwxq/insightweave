<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  clearPublishedData,
  deleteDataSource,
  getDeletionTask,
  getSourceDeletionPreview,
  parseBlockingRuns,
  parseDeletionTargetChange,
  retryDeletionTask,
} from '../api/dataSources'
import type { DeletionTaskDetail, DeletionTaskItem, DeletionRunRef, DeletionTargetChange } from '../api/types'
import BlockingRunList from './BlockingRunList.vue'
import DeletionTargetChangeNotice from './DeletionTargetChangeNotice.vue'

/**
 * doc 8.1 §2.4/§4/§7.0/§7.2：删除确认（输入源 ID）与清理已发布数据共用弹窗。
 *
 * - resumeTaskId：从对象响应的 deletion_task 摘要找回任务（刷新后同样适用），
 *   打开即拉取任务详情并恢复轮询，不重新提交 DELETE。
 * - 重试依据后端 can_retry（pending 认领超时、running 遗留也可恢复），
 *   不能只看 failed；can_retry=false 时展示 retry_reason。
 * - submitting 只表示提交/重试请求在途；后台任务状态由 task.status 展示，
 *   弹窗随时可关（关闭不取消任务）。
 * - 轮询为上一轮完成后再安排下一轮，失败不静默：展示提示并保留
 *   “重新获取进度”与关闭入口。
 */
const props = defineProps<{
  sourceId: string
  displayName: string
  /** clear：清理已发布数据（保留作业与采集文件）；delete：删除整个数据源 */
  mode?: 'delete' | 'clear'
  /** 从 deletion_task 摘要恢复的任务 ID（doc 8.1 §7.0） */
  resumeTaskId?: string
}>()

const emit = defineEmits<{
  cancel: []
  done: [action: string]
}>()

const mode = computed(() => props.mode ?? 'delete')
const confirmInput = ref('')
const submitting = ref(false)
const error = ref('')
const blockingRuns = ref<DeletionRunRef[]>([])
const checkingRuns = ref(false)
const blockingCheckError = ref('')
const blockingCleared = ref(false)
const pollError = ref('')
const taskId = ref<string | null>(props.resumeTaskId ?? null)
const task = ref<DeletionTaskDetail | null>(null)
const targetChange = ref<DeletionTargetChange | null>(null)
const visibleSteps = computed(() =>
  Object.entries(task.value?.steps ?? {}).filter(([name]) => !name.startsWith('_')),
)
let timer: number | null = null
let blockingTimer: number | null = null
let blockingVersion = 0
let disposed = false

const confirmMatches = computed(() => confirmInput.value.trim() === props.sourceId)

const scopeText =
  mode.value === 'clear'
    ? '将删除该源已发布画像和 RAG 数据；作业和采集文件保留，清理成功后可重新发布。'
    : '同时删除全部下属工作区与作业，清理该源画像及 RAG 数据；源业务数据库不受影响。'

function close() {
  if (submitting.value) return
  if (targetChange.value) {
    targetChange.value = null
    error.value = ''
    return
  }
  emit('cancel')
}

function stopPolling() {
  if (timer !== null) {
    window.clearTimeout(timer)
    timer = null
  }
}

function schedulePoll(taskId: string) {
  if (disposed) return
  stopPolling()
  timer = window.setTimeout(async () => {
    await fetchDetail(taskId, { keepPolling: true })
  }, 2000)
}

function stopBlockingTimer() {
  if (blockingTimer !== null) window.clearTimeout(blockingTimer)
  blockingTimer = null
}

function resetBlockingCheck() {
  stopBlockingTimer()
  blockingVersion += 1
  checkingRuns.value = false
  blockingCheckError.value = ''
  blockingCleared.value = false
}

function scheduleBlockingCheck() {
  stopBlockingTimer()
  if (disposed || taskId.value || !blockingRuns.value.length) return
  blockingTimer = window.setTimeout(() => void refreshBlockingRuns(), 3000)
}

async function refreshBlockingRuns() {
  if (disposed || submitting.value || checkingRuns.value || taskId.value) return
  stopBlockingTimer()
  const version = blockingVersion
  checkingRuns.value = true
  blockingCheckError.value = ''
  try {
    const preview = await getSourceDeletionPreview(props.sourceId)
    if (disposed || version !== blockingVersion || taskId.value) return
    if (!Array.isArray(preview.blocking_runs)) throw new Error('无法读取最新运行状态，请重新检查。')
    blockingRuns.value = preview.blocking_runs
    if (!blockingRuns.value.length) {
      error.value = ''
      blockingCleared.value = true
    }
  } catch (cause) {
    if (!disposed && version === blockingVersion) {
      blockingCheckError.value = cause instanceof Error ? cause.message : '运行状态检查失败，请重新检查。'
    }
  } finally {
    if (!disposed && version === blockingVersion) {
      checkingRuns.value = false
      scheduleBlockingCheck()
    }
  }
}

/** 首次获取与轮询共用：终态停轮询并通知父级，非终态继续。 */
async function fetchDetail(taskId: string, { keepPolling = false } = {}) {
  try {
    const detail = await getDeletionTask(taskId)
    if (disposed) return
    pollError.value = ''
    task.value = detail
    if (detail.status === 'success') {
      stopPolling()
      emit('done', mode.value)
    } else if (detail.status === 'failed') {
      stopPolling()
    } else if (keepPolling) {
      schedulePoll(taskId)
    }
  } catch (e) {
    if (disposed) return
    // 轮询失败不静默：停止本轮，保留重新获取与关闭入口
    stopPolling()
    pollError.value = e instanceof Error ? e.message : '任务进度获取失败'
  }
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') close()
}

async function submit() {
  if (submitting.value || !confirmMatches.value) return
  submitting.value = true
  resetBlockingCheck()
  error.value = ''
  blockingRuns.value = []
  try {
    const result: DeletionTaskItem =
      mode.value === 'clear'
        ? await clearPublishedData(props.sourceId, confirmInput.value.trim())
        : await deleteDataSource(props.sourceId, confirmInput.value.trim())
    taskId.value = result.task_id
    await fetchDetail(result.task_id, { keepPolling: true })
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    blockingRuns.value = parseBlockingRuns(e)
    scheduleBlockingCheck()
  } finally {
    submitting.value = false
  }
}

async function retry(confirmed = false) {
  if (!task.value || submitting.value) return
  submitting.value = true
  resetBlockingCheck()
  error.value = ''
  blockingRuns.value = []
  try {
    const change = targetChange.value
    const confirmation = confirmed && change ? {
      previous_fingerprint: change.previous.fingerprint,
      current_fingerprint: change.current.fingerprint,
    } : undefined
    const again = await retryDeletionTask(task.value.task_id, confirmation)
    targetChange.value = null
    task.value = { ...task.value, ...again, can_retry: false, retry_reason: '正在排队' }
    taskId.value = again.task_id
    await fetchDetail(again.task_id, { keepPolling: true })
  } catch (e) {
    const change = parseDeletionTargetChange(e)
    if (change) {
      stopPolling()
      targetChange.value = change
      return
    }
    // 409（保护期/锁竞争）等：以服务端结论为准，刷新详情并展示原因
    error.value = e instanceof Error ? e.message : String(e)
    blockingRuns.value = parseBlockingRuns(e)
    if (task.value) await fetchDetail(task.value.task_id, { keepPolling: true })
  } finally {
    submitting.value = false
  }
}

async function refreshProgress() {
  if (taskId.value && !submitting.value) {
    await fetchDetail(taskId.value, { keepPolling: true })
  }
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  if (taskId.value) {
    void fetchDetail(taskId.value, { keepPolling: true })
  }
})
onUnmounted(() => {
  disposed = true
  resetBlockingCheck()
  window.removeEventListener('keydown', onKeydown)
  stopPolling()
})
</script>

<template>
  <div class="dialog-backdrop" @click.self="close">
    <div class="dialog-panel" :class="{ 'target-change-panel': targetChange }" role="dialog" aria-modal="true" aria-labelledby="delete-source-title">
      <p id="delete-source-title">
        <template v-if="targetChange">存储目标已变化：{{ displayName }}（{{ sourceId }}）</template>
        <template v-else>
        {{ mode === 'clear' ? '清理已发布数据' : '删除数据源' }}“{{ displayName }}”（{{ sourceId }}）？
        </template>
      </p>
      <DeletionTargetChangeNotice
        v-if="targetChange"
        :key="targetChange.previous.fingerprint + targetChange.current.fingerprint"
        :change="targetChange"
        :mode="mode"
        :busy="submitting"
        @cancel="close"
        @confirm="retry(true)"
      />
      <template v-else>
      <template v-if="!taskId">
        <p class="dialog-note">{{ scopeText }}</p>
        <p class="dialog-note">
          输入数据源 ID（{{ sourceId }}）确认；删除完成后可重新创建同名数据源，新对象不继承历史。
        </p>
        <input
          v-model="confirmInput"
          class="dialog-input"
          :placeholder="sourceId"
          :disabled="submitting"
          autocomplete="off"
        />
      </template>
      <div v-else-if="task" class="dialog-note">
        <p>
          任务状态：{{ task.status === 'pending' ? '等待执行' : task.status === 'running' ? '执行中' : task.status === 'success' ? '已完成' : '失败' }}
        </p>
        <p v-for="[step, payload] in visibleSteps" :key="step" class="dialog-note">
          {{ step }}：{{ (payload as Record<string, unknown>)?.status ?? '—' }}
        </p>
        <p v-if="task.error_message" class="status-message" role="alert">{{ task.error_message }}</p>
        <p v-if="!task.can_retry" class="dialog-note">{{ task.retry_reason ?? '处理中' }}</p>
      </div>
      <p v-else class="dialog-note">任务已受理（{{ taskId }}），正在获取进度…</p>
      <p v-if="pollError" class="status-message" role="alert">
        {{ pollError }}
      </p>
      <BlockingRunList :runs="blockingRuns" />
      <div v-if="blockingRuns.length && !taskId" class="blocking-check-actions">
        <span class="dialog-note">每 3 秒自动检查运行状态</span>
        <button type="button" class="button button-outline" :disabled="checkingRuns || submitting" @click="refreshBlockingRuns">
          {{ checkingRuns ? '正在检查…' : '重新检查运行状态' }}
        </button>
      </div>
      <p v-if="blockingCheckError" class="status-message" role="alert">{{ blockingCheckError }}</p>
      <p v-if="blockingCleared && !taskId" class="status-message success" role="status">
        阻断运行已全部结束，可以重新确认{{ mode === 'clear' ? '清理' : '删除' }}。
      </p>
      <div class="dialog-actions">
        <button type="button" class="button button-outline" :disabled="submitting" @click="close">
          {{ task?.status === 'success' ? '关闭' : '取消' }}
        </button>
        <button
          v-if="!taskId"
          type="button"
          class="button button-primary"
          :disabled="submitting || !confirmMatches || blockingRuns.length > 0"
          @click="submit"
        >
          {{ mode === 'clear' ? '确认清理' : '确认删除' }}
        </button>
        <template v-if="taskId">
          <template v-if="task && task.status !== 'success'">
          <button
            v-if="task.can_retry"
            type="button"
            class="button button-primary"
            :disabled="submitting"
            @click="retry()"
          >
            {{ task.status === 'failed' ? '重试' : '检查并重试' }}
          </button>
          <button
            v-else
            type="button"
            class="button button-outline"
            disabled
            :title="task.retry_reason ?? ''"
          >
            {{ task.status === 'failed' ? '等待处理' : '处理中' }}
          </button>
          </template>
          <button
            v-if="pollError"
            type="button"
            class="button button-outline"
            :disabled="submitting"
            @click="refreshProgress"
          >
            重新获取进度
          </button>
        </template>
      </div>
      </template>
      <p v-if="error" class="status-message" role="alert">{{ error }}</p>
    </div>
  </div>
</template>

<style scoped>
.target-change-panel { width: min(100%, 760px); max-height: calc(100dvh - 48px); overflow-y: auto; }
.blocking-check-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin: 12px 0; }
.blocking-check-actions .dialog-note { font-size: 12px; color: #66788f; }
.blocking-check-actions .button { min-height: 32px; font-size: 12px; }
</style>
