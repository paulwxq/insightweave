<script setup lang="ts">
/**
 * 运行步骤详情抽屉（doc 6.3 §4 编号 26.2/27.2、§5.3）：
 * 状态、参数、耗时、错误；采集步骤展示「读取的文件 / 生成的文件」，
 * 发布步骤展示「读取的文件 / 处理的下游目标」。manifest 为空显示
 * 「暂无记录」；目标出现在清单中不代表发布成功，结果以步骤状态为准。
 * 只读展示，不修改作业定义。
 */
import { computed } from 'vue'
import { displayDate } from '../api/format'
import type { JobKind, RunDetail, RunStepItem } from '../api/types'
import {
  manifestFiles,
  manifestOutputFiles,
  manifestTargets,
  stepDuration,
  targetIdentity,
  targetTypeLabel,
} from '../utils/manifests'
import { stepStatusText } from '../utils/steps'

const props = defineProps<{
  kind: JobKind
  run: RunDetail | null
  /** 当前展示的步骤；不在运行步骤内时仅显示空态 */
  stepName: string
  loading: boolean
  error: string
}>()

const emit = defineEmits<{
  close: []
  'select-step': [step: string]
}>()

const step = computed<RunStepItem | null>(() => {
  if (!props.run) return null
  return props.run.steps.find((row) => row.step_name === props.stepName) ?? null
})

const inputFiles = computed(() => manifestFiles(step.value?.input_manifest))
const outputFiles = computed(() => manifestOutputFiles(step.value?.output_manifest))
const targets = computed(() => manifestTargets(step.value?.target_manifest))

/** 生效参数（run_step 记录解析后的实际参数，doc 6.2 §6.2） */
const paramLines = computed(() => {
  const config = step.value?.step_config
  if (!config) return []
  return Object.entries(config).map(([key, value]) => `${key} = ${JSON.stringify(value)}`)
})

const duration = computed(() =>
  stepDuration(step.value?.started_at, step.value?.finished_at),
)

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
}
</script>

<template>
  <div class="dialog-backdrop" @click.self="emit('close')" @keydown="onKeydown">
    <div
      class="dialog-panel with-close step-drawer"
      role="dialog"
      aria-modal="true"
      aria-label="步骤详情"
      tabindex="0"
    >
      <button type="button" class="dialog-close" aria-label="关闭" @click="emit('close')"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></button>

      <template v-if="loading && !run">
        <p class="ds-jobs-state">正在读取运行详情…</p>
      </template>
      <template v-else-if="error">
        <p class="ds-jobs-state error">
          {{ error }}
        </p>
      </template>
      <template v-else-if="run">
        <p class="step-drawer-kicker ds-kicker">
          {{ kind === 'collection' ? 'COLLECTION STEP' : 'PUBLISH STEP' }}
        </p>

        <!-- 步骤切换：同一运行内的步骤可切换查看 -->
        <div v-if="run.steps.length > 0" class="step-switch" role="tablist" aria-label="步骤切换">
          <button
            v-for="row in run.steps"
            :key="row.step_name"
            type="button"
            role="tab"
            class="step-switch-chip"
            :class="{ current: row.step_name === stepName }"
            :aria-selected="row.step_name === stepName"
            @click="emit('select-step', row.step_name)"
          >
            {{ row.step_name }}
          </button>
        </div>

        <template v-if="step">
          <div class="step-drawer-head">
            <h2 class="step-drawer-title">{{ step.step_name }}</h2>
            <span class="home-status" :class="step.status">{{ stepStatusText(step.status) }}</span>
          </div>

          <div class="step-drawer-facts">
            <p><span>步骤状态</span><strong>{{ stepStatusText(step.status) }}</strong></p>
            <p><span>开始时间</span><strong>{{ displayDate(step.started_at ?? '') }}</strong></p>
            <p><span>结束时间</span><strong>{{ displayDate(step.finished_at ?? '') }}</strong></p>
            <p><span>耗时</span><strong>{{ duration || '—' }}</strong></p>
          </div>

          <p v-if="step.message" class="step-drawer-message" :class="step.status" role="alert">
            {{ step.message }}
          </p>

          <section class="step-drawer-section">
            <h3>生效参数</h3>
            <pre v-if="paramLines.length" class="step-drawer-code">{{ paramLines.join('\n') }}</pre>
            <p v-else class="step-drawer-empty">无参数记录</p>
          </section>

          <section class="step-drawer-section">
            <h3>读取的文件</h3>
            <ul v-if="inputFiles.length" class="step-drawer-files">
              <li v-for="file in inputFiles" :key="file.path">
                <span class="step-file-path">{{ file.path }}</span>
                <small v-if="file.collection_run_id" class="step-file-run">
                  采集运行 {{ file.collection_run_id }}
                </small>
              </li>
            </ul>
            <p v-else class="step-drawer-empty">暂无记录</p>
          </section>

          <section class="step-drawer-section">
            <h3 v-if="kind === 'collection'">生成的文件</h3>
            <h3 v-else>处理的下游目标</h3>
            <ul v-if="kind === 'collection' && outputFiles.length" class="step-drawer-files">
              <li v-for="path in outputFiles" :key="path">
                <span class="step-file-path">{{ path }}</span>
              </li>
            </ul>
            <ul v-else-if="kind === 'publish' && targets.length" class="step-drawer-files">
              <li v-for="(target, index) in targets" :key="`${target.type}-${index}`">
                <span class="step-target-type">{{ targetTypeLabel(target.type) }}</span>
                <span class="step-file-path">{{ targetIdentity(target) }}</span>
              </li>
            </ul>
            <p v-else class="step-drawer-empty">暂无记录</p>
            <p v-if="kind === 'publish'" class="step-drawer-note">
              清单记录本次处理的下游目标；目标出现不代表发布成功，成功与否以步骤状态为准。
            </p>
          </section>
        </template>
        <p v-else class="step-drawer-empty">
          该运行没有步骤 {{ stepName }} 的记录；请切换上方步骤查看。
        </p>
      </template>
      <template v-else>
        <p class="step-drawer-empty">暂无运行详情</p>
      </template>
    </div>
  </div>
</template>

<style scoped>
.step-drawer {
  width: min(100%, 560px);
  max-height: min(88vh, 860px);
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.step-drawer-kicker {
  margin-bottom: -6px;
}
.step-switch {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.step-switch-chip {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  border: 1px solid #d9e3ef;
  border-radius: 8px;
  background: #fff;
  color: #66788f;
  padding: 5px 10px;
}
.step-switch-chip.current {
  background: #e3eefb;
  border-color: #2e7cd6;
  color: #2e7cd6;
  font-weight: 700;
}
.step-drawer-head {
  display: flex;
  align-items: center;
  gap: 12px;
}
.step-drawer-title {
  margin: 0;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 20px;
  color: #17293f;
}
.step-drawer-facts {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  border: 1px solid #e1e9f4;
  border-radius: 12px;
  background: #fcfdff;
  padding: 12px 14px;
}
.step-drawer-facts p {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.step-drawer-facts span {
  color: #66788f;
  font-size: 11px;
}
.step-drawer-facts strong {
  font-size: 12px;
  color: #17293f;
  overflow-wrap: anywhere;
}
.step-drawer-message {
  margin: 0;
  border-radius: 10px;
  padding: 10px 12px;
  font-size: 12px;
  overflow-wrap: anywhere;
  background: #eef3f9;
  color: #17293f;
}
.step-drawer-message.failed {
  background: #fbecec;
  color: #c64f4f;
}
.step-drawer-message.blocked {
  background: #f8efd9;
  color: #d97706;
}
.step-drawer-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.step-drawer-section h3 {
  margin: 0;
  font-size: 13px;
  color: #17293f;
}
.step-drawer-code {
  margin: 0;
  padding: 10px 12px;
  background: #f7fafc;
  border: 1px solid #e1e9f4;
  border-radius: 10px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  color: #17293f;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.step-drawer-files {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.step-drawer-files li {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  border: 1px solid #e1e9f4;
  border-radius: 10px;
  background: #fcfdff;
  padding: 8px 11px;
}
.step-file-path {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  color: #17293f;
  overflow-wrap: anywhere;
}
.step-file-run {
  color: #66788f;
  font-size: 10px;
  overflow-wrap: anywhere;
}
.step-target-type {
  flex-shrink: 0;
  border-radius: 8px;
  background: #e3eefb;
  color: #2e7cd6;
  font-size: 10px;
  font-weight: 700;
  padding: 3px 8px;
}
.step-drawer-empty {
  margin: 0;
  border: 1px dashed #d9e3ef;
  border-radius: 10px;
  background: #f7fafc;
  color: #66788f;
  font-size: 12px;
  padding: 10px 12px;
}
.step-drawer-note {
  margin: 0;
  color: #66788f;
  font-size: 11px;
}
</style>
