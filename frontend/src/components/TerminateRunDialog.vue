<script setup lang="ts">
/**
 * 终止运行确认弹窗（doc 11 §4.4.2）。打开时不调用任何接口，按固定规则
 * 从已加载的运行详情直接展示计划；必填原因 + 勾选承诺后才能确认终止。
 * 409（本进程执行中/注册残留）保留弹窗并展示拒绝文案；成功后由调用方
 * 立即刷新运行详情（useRunPolling.refresh）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ApiError } from '../api/http'
import { resolveRun } from '../api/workspaces'
import type { JobKind, RunStepItem } from '../api/types'
import { stepStatusText } from '../utils/steps'
import { canSubmitTerminate, plannedFinalStatus, plannedStepTransition } from '../utils/terminateRun'

const props = defineProps<{
  kind: JobKind
  runId: string
  steps: RunStepItem[]
}>()

const emit = defineEmits<{ cancel: []; success: [] }>()

const reason = ref('')
const confirmed = ref(false)
const submitting = ref(false)
const error = ref('')
const rejected = ref('')

const canSubmit = computed(
  () => canSubmitTerminate(reason.value, confirmed.value) && !submitting.value,
)

const finalStatusText = computed(() => {
  const plan = plannedFinalStatus(props.steps)
  if (plan === 'success') return '成功'
  if (plan === 'partial_success') return '部分成功'
  if (plan === 'failed') return '失败'
  return '—（运行没有步骤）'
})

function close() {
  if (submitting.value) return
  emit('cancel')
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') close()
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  error.value = ''
  rejected.value = ''
  try {
    await resolveRun(props.kind, props.runId, {
      reason: reason.value.trim(),
      confirm_executors_stopped: true,
    })
    emit('success')
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      rejected.value = e.message
    } else {
      error.value = e instanceof Error ? e.message : String(e)
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="dialog-backdrop" @click.self="close">
    <div class="dialog-panel" role="dialog" aria-modal="true" aria-labelledby="terminate-run-title">
      <p id="terminate-run-title">终止运行</p>
      <p class="dialog-note">
        终止仅用于<strong>执行进程已不存在</strong>的中断运行。本功能只修改运行与步骤的状态记录，
        不停止线程、不删除产物文件、不改 marker、不清理已发布内容。
        若运行正在本进程执行中，后端将拒绝本操作。
      </p>
      <ul class="terminate-plan">
        <li v-for="step in steps" :key="step.step_name">
          <span class="terminate-step-name">{{ step.step_name }}</span>
          <span>{{ stepStatusText(step.status) }} → {{ plannedStepTransition(step.status) }}</span>
        </li>
      </ul>
      <p class="dialog-note">
        整体终态（按当前状态推算）：{{ finalStatusText }}；实际终态以提交时后端裁决为准。
      </p>
      <label class="field terminate-reason">
        <span>处理原因（必填）</span>
        <textarea
          v-model="reason"
          rows="3"
          placeholder="如：后端进程被回收，rel 中断收尾"
          :disabled="submitting"
        ></textarea>
      </label>
      <label class="dialog-check">
        <input v-model="confirmed" type="checkbox" :disabled="submitting" />
        <span>我已确认执行该运行的进程已停止</span>
      </label>
      <p v-if="rejected" class="status-message" role="alert">{{ rejected }}</p>
      <p v-if="error" class="status-message" role="alert">{{ error }}</p>
      <div class="dialog-actions">
        <button type="button" class="button button-outline" :disabled="submitting" @click="close">
          取消
        </button>
        <button type="button" class="button button-primary" :disabled="!canSubmit" @click="submit">
          确认终止
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.terminate-plan {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 12px;
}
.terminate-plan li {
  display: flex;
  gap: 12px;
  align-items: baseline;
}
.terminate-step-name {
  flex: 0 0 84px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-weight: 700;
}
.terminate-reason textarea {
  width: 100%;
  min-height: 74px;
  border: 1px solid #d2e0f2;
  border-radius: 10px;
  background: #f1f7ff;
  color: #20364f;
  padding: 9px 12px;
  outline: none;
  resize: vertical;
}
.terminate-reason textarea:focus {
  border-color: #3a83dc;
}
</style>
