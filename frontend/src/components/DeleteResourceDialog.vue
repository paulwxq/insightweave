<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { getDeletionTask, parseBlockingRuns, retryDeletionTask } from '../api/dataSources'
import type { DeletionTaskDetail, DeletionRunRef } from '../api/types'
import { deleteJob, deleteWorkspace, getWorkspaceDeletionPreview } from '../api/workspaces'
import BlockingRunList from './BlockingRunList.vue'

/**
 * doc 8.1 §4/§5.2/§7.0/§7.2：工作区删除（输入 ID 确认、202 任务）与作业删除
 * （二次确认、204）共用弹窗。
 *
 * - resumeTaskId：从对象响应的 deletion_task 摘要找回工作区删除任务。
 * - 重试依据后端 can_retry（pending/running 遗留同样可恢复）。
 * - submitting 仅表示请求在途；后台任务状态由 task.status 展示，
 *   弹窗随时可关（关闭不取消任务）。
 * - 轮询上一轮完成后再安排下一轮；失败展示提示并可手动重新获取。
 */
const props = defineProps<{
  kind: 'workspace' | 'job'
  targetId: string
  displayName: string
  jobKind?: 'collection' | 'publish'
  /** 从 deletion_task 摘要恢复的任务 ID（doc 8.1 §7.0） */
  resumeTaskId?: string
}>()

const emit = defineEmits<{
  cancel: []
  done: []
}>()

const submitting = ref(false)
const error = ref('')
const pollError = ref('')
const confirmInput = ref('')
const taskId = ref<string | null>(props.resumeTaskId ?? null)
const task = ref<DeletionTaskDetail | null>(null)
const blockingRuns = ref<DeletionRunRef[]>([])
let timer: number | null = null
let disposed = false

const needsConfirmId = computed(() => props.kind === 'workspace')
const confirmMatches = computed(
  () => !needsConfirmId.value || confirmInput.value.trim() === props.targetId,
)

function close() {
  if (submitting.value) return
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

async function fetchDetail(taskId: string, { keepPolling = false } = {}) {
  try {
    const detail = await getDeletionTask(taskId)
    if (disposed) return
    pollError.value = ''
    task.value = detail
    if (detail.status === 'success') {
      stopPolling()
      emit('done')
    } else if (detail.status === 'failed') {
      stopPolling()
    } else if (keepPolling) {
      schedulePoll(taskId)
    }
  } catch (e) {
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
  error.value = ''
  blockingRuns.value = []
  try {
    if (props.kind === 'job') {
      await deleteJob(props.jobKind ?? 'collection', props.targetId)
      emit('done')
      return
    }
    const result = await deleteWorkspace(props.targetId, confirmInput.value.trim())
    taskId.value = result.task_id
    await fetchDetail(result.task_id, { keepPolling: true })
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    blockingRuns.value = parseBlockingRuns(e)
  } finally {
    submitting.value = false
  }
}

async function retry() {
  if (!task.value || submitting.value) return
  submitting.value = true
  error.value = ''
  blockingRuns.value = []
  try {
    const again = await retryDeletionTask(task.value.task_id)
    taskId.value = again.task_id
    await fetchDetail(again.task_id, { keepPolling: true })
  } catch (e) {
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

onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  if (taskId.value) {
    await fetchDetail(taskId.value, { keepPolling: true })
    return
  }
  if (props.kind === 'workspace' && needsConfirmId.value) {
    try {
      const preview = await getWorkspaceDeletionPreview(props.targetId)
      blockingRuns.value = preview.blocking_runs
    } catch {
      /* 预览失败不阻断确认 */
    }
  }
})
onUnmounted(() => {
  disposed = true
  window.removeEventListener('keydown', onKeydown)
  stopPolling()
})
</script>

<template>
  <div class="dialog-backdrop" @click.self="close">
    <div class="dialog-panel" role="dialog" aria-modal="true" aria-labelledby="delete-resource-title">
      <p id="delete-resource-title">
        {{ kind === 'workspace' ? '删除工作区' : '删除作业' }}“{{ displayName }}”（{{ targetId }}）？
      </p>
      <p v-if="!task" class="dialog-note">
        {{
          kind === 'workspace'
            ? '同时删除下属作业和工作区采集文件；该源已发布数据保留。'
            : '删除后不能再执行；历史记录、工作区文件及已发布数据保留。'
        }}
      </p>
      <BlockingRunList :runs="blockingRuns" />
      <input
        v-if="needsConfirmId && !task"
        v-model="confirmInput"
        class="dialog-input"
        :placeholder="targetId"
        :disabled="submitting"
        autocomplete="off"
      />
      <div v-if="task" class="dialog-note">
        <p>
          任务状态：{{ task.status === 'pending' ? '等待执行' : task.status === 'running' ? '执行中' : task.status === 'success' ? '已完成' : '失败' }}
        </p>
        <p v-if="task.error_message" class="status-message" role="alert">{{ task.error_message }}</p>
        <p v-if="!task.can_retry" class="dialog-note">{{ task.retry_reason ?? '处理中' }}</p>
      </div>
      <p v-else class="dialog-note">任务已受理（{{ taskId }}），正在获取进度…</p>
      <p v-if="pollError" class="status-message" role="alert">{{ pollError }}</p>
      <p v-if="error" class="status-message" role="alert">{{ error }}</p>
      <div class="dialog-actions">
        <button type="button" class="button button-outline" :disabled="submitting" @click="close">
          {{ task?.status === 'success' ? '关闭' : '取消' }}
        </button>
        <button
          v-if="!taskId"
          type="button"
          class="button button-primary"
          :disabled="submitting || !confirmMatches"
          @click="submit"
        >
          确认删除
        </button>
        <template v-if="taskId">
          <template v-if="task && task.status !== 'success'">
          <button
            v-if="task.can_retry"
            type="button"
            class="button button-primary"
            :disabled="submitting"
            @click="retry"
          >
            {{ task.status === 'failed' ? '重试' : '检查并重试' }}
          </button>
          <button v-else type="button" class="button button-outline" disabled>
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
    </div>
  </div>
</template>
