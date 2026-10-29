<script setup lang="ts">
/**
 * 运行详情面板（采集/发布通用）：展示整体状态与逐步骤结果，
 * 未到终态时每 3 秒自动刷新；blocked 不混为失败，partial_success 单列。
 */
import { computed } from 'vue'
import { displayDate } from '../api/format'
import type { JobKind, RunDetail, RunStepItem } from '../api/types'
import { runStatusText, stepStatusText } from '../utils/steps'
import { stepDuration } from '../utils/manifests'

const props = defineProps<{
  kind: JobKind
  run: RunDetail | null
  loading: boolean
  error: string
}>()

const emit = defineEmits<{
  retry: []
  /** 点击步骤行 → 打开步骤详情抽屉（doc 6.3 §4 26.2/27.2） */
  'open-step': [step: string]
}>()

const runUuid = computed(() =>
  props.run
    ? props.run.collection_run_id ?? props.run.publish_run_id ?? ''
    : '',
)
const pendingState = computed(
  () => props.run?.status === 'pending' || props.run?.status === 'running',
)

function triggerText(trigger: string) {
  if (trigger === 'manual') return '手动'
  if (trigger === 'scheduled') return '定时'
  return trigger
}

function stepConfigText(step: { step_name: string; step_config: Record<string, unknown> | null }) {
  // 发布参数快照仅在步骤详情展示，避免完整数组挤占执行结果。
  if (props.kind === 'publish' || !step.step_config) return ''
  const parts: string[] = []
  for (const [key, value] of Object.entries(step.step_config)) {
    // doc 7.2 §5.3：运行快照含 dim_value.tables / domain.domains 完整清单（数组），
    // 非字符串值用 JSON 展示实际参数，避免 [object Object]
    parts.push(`${key}=${typeof value === 'string' ? value : JSON.stringify(value)}`)
  }
  return parts.join(' · ')
}

function stepDurationText(step: RunStepItem) {
  const finished = stepDuration(step.started_at, step.finished_at)
  if (finished) return finished
  if (step.started_at && (step.status === 'running' || step.status === 'pending')) {
    return stepDuration(step.started_at, new Date().toISOString()) || '—'
  }
  return '—'
}

function stepTitle(step: { step_name: string; message: string | null; step_config: Record<string, unknown> | null }) {
  const parts = [`查看步骤 ${step.step_name} 详情`]
  if (step.message) parts.push(step.message)
  const config = stepConfigText(step)
  if (config) parts.push(config)
  return parts.join('\n')
}
</script>

<template>
  <div class="run-panel">
    <p v-if="loading && !run" class="ds-jobs-state">正在读取运行详情…</p>
    <p v-else-if="error" class="ds-jobs-state error">
      {{ error }}
      <button type="button" class="ds-new-job-edit" @click="emit('retry')">重试</button>
    </p>
    <template v-else-if="run">
      <div class="run-summary">
        <div class="run-summary-top">
          <strong>{{ run.job_name || run.job_id }}</strong>
          <span class="home-status" :class="run.status">{{ runStatusText(run.status) }}</span>
          <span>版本 {{ run.revision_no }}</span>
          <span>{{ triggerText(run.trigger_type) }}</span>
        </div>
        <p class="run-summary-id">
          <span>运行 ID</span>
          <strong :title="runUuid">{{ runUuid }}</strong>
        </p>
        <div class="run-summary-times">
          <p><span>创建</span><strong>{{ displayDate(run.created_at ?? '') || '—' }}</strong></p>
          <p><span>开始</span><strong>{{ displayDate(run.started_at ?? '') || '—' }}</strong></p>
          <p><span>结束</span><strong>{{ displayDate(run.finished_at ?? '') || '—' }}</strong></p>
        </div>
      </div>
      <p v-if="run.message" class="run-panel-message" :class="{ bad: run.status !== 'success' }">
        {{ run.message }}
      </p>
      <ul class="run-steps">
        <li v-for="step in run.steps" :key="step.step_name" :class="step.status">
          <button
            type="button"
            class="run-step-main"
            :title="stepTitle(step)"
            @click="emit('open-step', step.step_name)"
          >
            <span class="run-step-name">{{ step.step_name }}</span>
            <span class="home-status" :class="step.status">
              {{ stepStatusText(step.status) }}
            </span>
            <span class="run-step-time">{{ stepDurationText(step) }}</span>
            <span class="run-step-detail">
              <span v-if="step.message">{{ step.message }}</span>
              <span v-if="stepConfigText(step)" class="run-step-config">{{ stepConfigText(step) }}</span>
            </span>
          </button>
        </li>
      </ul>
      <p class="run-panel-note">点击步骤行可查看步骤详情（参数、读取/生成文件与下游目标）。</p>
      <p v-if="pendingState" class="run-panel-note" role="status">
        运行尚未结束，每 3 秒自动刷新；离开本页不会取消执行。
      </p>
    </template>
  </div>
</template>

<style scoped>
.run-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  font-size: 13px;
}
.run-summary {
  display: flex;
  flex-direction: column;
  gap: 10px;
  border: 1px solid #e1e9f4;
  border-radius: 12px;
  background: #fcfdff;
  padding: 12px 14px;
}
.run-summary-top {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.run-summary-top strong {
  color: #17293f;
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.run-summary-top > span:not(.home-status) {
  color: #66788f;
  font-size: 12px;
  white-space: nowrap;
}
.run-summary-id {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0;
  min-width: 0;
}
.run-summary-id span {
  flex-shrink: 0;
  color: #66788f;
  font-size: 11px;
  letter-spacing: 0.04em;
}
.run-summary-id strong {
  min-width: 0;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.run-summary-times {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.run-summary-times p {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  background: #eef3f9;
  border-radius: 8px;
  padding: 8px 10px;
}
.run-summary-times span {
  color: #66788f;
  font-size: 10px;
  letter-spacing: 1px;
}
.run-summary-times strong {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.run-panel-message {
  margin: 0;
  padding: 9px 12px;
  border-radius: 10px;
  background: #eef3f9;
  color: #17293f;
  overflow-wrap: anywhere;
}
.run-panel-message.bad {
  background: #fbecec;
  color: #c64f4f;
}
.run-steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.run-steps li {
  border: 1px solid #e1e9f4;
  border-radius: 10px;
  background: #fcfdff;
  overflow: hidden;
}
.run-step-main {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  min-width: 0;
  text-align: left;
  padding: 8px 12px;
  background: transparent;
  border: 0;
  white-space: nowrap;
}
.run-step-main:hover .run-step-name {
  color: #2e7cd6;
}
.run-steps li.failed {
  border-color: #efcaca;
}
.run-steps li.success {
  border-color: #cfe8de;
}
.run-step-name {
  flex: 0 0 72px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-weight: 700;
}
.run-step-time {
  flex: 0 0 64px;
  color: #66788f;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.run-step-detail {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1 1 auto;
  min-width: 0;
  color: #66788f;
  font-size: 12px;
}
.run-step-detail > span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.run-step-detail > span:first-child {
  flex: 1 1 auto;
}
.run-step-config {
  flex: 0 1 auto;
  color: #2e7cd6;
}
.run-panel-note {
  margin: 0;
  color: #66788f;
  font-size: 11px;
}
</style>
