<script setup lang="ts">
/**
 * 保存新版本编辑器（doc 6.2 §9.1.4）：编辑执行配置 → 新增不可变版本；
 * 旧版本不可原地更新。采集版本可保存 domain.description 默认输入。
 * 发布分支带发布校验门控（设计稿 34，与新建发布页同规则）：勾选、编辑
 * 与保存都要求对应步骤通过 publish-check；校验请求由父组件发起。
 */
import { computed, ref, watch } from 'vue'
import type { JobKind, PublishCheckStep } from '../api/types'
import DimValueConfigDialog from './DimValueConfigDialog.vue'
import DomainConfigDialog from './DomainConfigDialog.vue'
import DomainDescriptionDialog from './DomainDescriptionDialog.vue'
import RelEditDialog from './RelEditDialog.vue'
import StepGrid from './StepGrid.vue'
import { allSteps, unchosenDeps } from '../utils/steps'

const props = defineProps<{
  kind: JobKind
  /** 编辑起点（通常为当前查看版本） */
  initialSteps: string[]
  initialStepConfig: Record<string, unknown> | null
  saving: boolean
  error: string
  /** 发布：Domain/Rel/Dim 配置编辑弹窗的工作区定位 */
  workspaceId?: string
  dbName?: string
  /** 发布：publish-check 结果（null=未校验，步骤全部置灰）；点击「发布校验」由父组件请求 */
  checkSteps?: PublishCheckStep[] | null
  checking?: boolean
}>()

const emit = defineEmits<{
  cancel: []
  save: [payload: { steps: string[]; step_config: Record<string, Record<string, unknown>> | null; change_note: string | null }]
  check: []
  recheck: []
}>()

const selected = ref<Record<string, boolean>>(
  Object.fromEntries(props.initialSteps.map((step) => [step, true])),
)
const changeNote = ref('')
const initialDomainDesc = props.initialStepConfig?.domain as
  | Record<string, unknown>
  | undefined
const savedDescription = ref<string | null>(
  typeof initialDomainDesc?.description === 'string' && initialDomainDesc.description
    ? initialDomainDesc.description
    : null,
)

const chosenSteps = computed(() => Object.keys(selected.value).filter((step) => selected.value[step]))
const depHints = computed(() =>
  props.kind === 'collection' ? unchosenDeps(chosenSteps.value) : [],
)
const descriptionOpen = ref(false)

// 发布校验门控（与新建发布页一致）：checkSteps 为 null 表示尚未校验
const checkRan = computed(() => props.checkSteps != null)
const checkByStep = computed(() => {
  const map = new Map<string, PublishCheckStep>()
  for (const step of props.checkSteps ?? []) map.set(step.step_name, step)
  return map
})
const passedSteps = computed(() =>
  (props.checkSteps ?? []).filter((step) => step.ready).map((step) => step.step_name),
)
const failedCount = computed(
  () => (props.checkSteps ?? []).filter((step) => !step.ready).length,
)

function stepCheck(step: string): PublishCheckStep | undefined {
  return checkByStep.value.get(step)
}

// 校验结果更新后剔除已不通过的勾选（如上游产物在编辑期间变化）
watch(
  () => props.checkSteps,
  (steps) => {
    if (props.kind !== 'publish' || !steps) return
    const passed = new Set<string>(
      steps.filter((step) => step.ready).map((step) => step.step_name),
    )
    const next: Record<string, boolean> = {}
    for (const [step, on] of Object.entries(selected.value)) next[step] = on && passed.has(step)
    selected.value = next
  },
)

const canSave = computed(() => {
  if (props.saving || chosenSteps.value.length === 0) return false
  if (props.kind !== 'publish') return true
  return chosenSteps.value.every((step) => stepCheck(step)?.ready === true)
})

function toggleStep(step: string) {
  if (props.kind === 'publish' && stepCheck(step)?.ready !== true) return
  selected.value = { ...selected.value, [step]: !selected.value[step] }
}

function selectAll() {
  const steps = props.kind === 'publish' ? passedSteps.value : allSteps(props.kind)
  selected.value = Object.fromEntries(steps.map((step) => [step, true]))
}

function onDescriptionSaved(text: string) {
  savedDescription.value = text
  descriptionOpen.value = false
}

// 发布配置文件编辑弹窗（工作区定位；保存写文件，不进版本）
const domainOpen = ref(false)
const relOpen = ref(false)
const dimOpen = ref(false)

function closeConfigDialogs() {
  domainOpen.value = false
  relOpen.value = false
  dimOpen.value = false
}

function onConfigSaved() {
  closeConfigDialogs()
  emit('recheck')
}

function submit() {
  if (!canSave.value) return
  const steps = chosenSteps.value
  const useDescription =
    props.kind === 'collection' && steps.includes('domain') && savedDescription.value !== null
  emit('save', {
    steps,
    step_config: useDescription ? { domain: { description: savedDescription.value! } } : null,
    change_note: changeNote.value.trim() || null,
  })
}

watch(
  () => props.initialSteps,
  (steps) => {
    selected.value = Object.fromEntries(steps.map((step) => [step, true]))
  },
)
</script>

<template>
  <div class="jc-editor">
    <div class="ds-new-job-step-actions jc-editor-actions">
      <template v-if="kind === 'publish'">
        <span
          v-if="checkRan"
          class="jc-check-summary"
          :class="failedCount > 0 ? 'bad' : 'ok'"
        >
          {{ failedCount > 0 ? `${failedCount} 项未通过` : `${passedSteps.length} 项通过` }}
        </span>
        <button type="button" :disabled="checking" @click="emit('check')">
          {{ checking ? '正在校验' : '发布校验' }}
        </button>
      </template>
      <button type="button" @click="selectAll">全选</button>
      <button type="button" @click="selected = {}">清空</button>
    </div>

    <p v-for="hint in depHints" :key="hint.step" class="ds-new-job-hint" role="status">
      {{ hint.step }} 未同选依赖步骤（{{ hint.deps.join('、') }}）：执行时将读取本工作区现有产物。
    </p>

    <StepGrid
      :kind="kind"
      :selected="selected"
      :available="kind === 'publish' ? passedSteps : undefined"
      :disabled="saving || checking"
      @toggle="toggleStep"
    >
      <template #after-step="{ step }">
        <button
          v-if="kind === 'collection' && step === 'domain'"
          type="button"
          class="nj-step-edit"
          :disabled="saving"
          @click="descriptionOpen = true"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
          编辑
        </button>
        <template v-if="kind === 'publish'">
          <span
            v-if="checkRan && stepCheck(step)?.ready"
            class="jc-check-mark ok"
            title="发布校验通过"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="m8.4 12.2 2.4 2.4 4.8-5.2" /></svg>
          </span>
          <span
            v-else-if="checkRan"
            class="jc-check-mark bad"
            :title="`发布校验未通过：${stepCheck(step)?.reason ?? '原因未知'}`"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 7.6v5.2" /><path d="M12 16.4v.2" /></svg>
          </span>
          <em
            v-if="checkRan && !stepCheck(step)?.ready"
            class="jc-step-reason"
            :title="stepCheck(step)?.reason ?? '原因未知'"
          >
            {{ stepCheck(step)?.reason ?? '原因未知' }}
          </em>
          <button
            v-if="step === 'domain'"
            type="button"
            class="nj-step-edit"
            :disabled="saving || checking || stepCheck(step)?.ready !== true"
            @click="domainOpen = true"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
            编辑
          </button>
          <button
            v-if="step === 'rel'"
            type="button"
            class="nj-step-edit"
            :disabled="saving || checking || stepCheck(step)?.ready !== true"
            @click="relOpen = true"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
            编辑
          </button>
          <button
            v-if="step === 'dim_value'"
            type="button"
            class="nj-step-edit"
            :disabled="saving || checking || stepCheck(step)?.ready !== true"
            @click="dimOpen = true"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
            编辑
          </button>
        </template>
      </template>
    </StepGrid>

    <label class="field jc-change-note">
      <span>变更说明（可选）</span>
      <input v-model="changeNote" type="text" placeholder="本次配置变更的原因" :disabled="saving" />
    </label>

    <p v-if="error" class="status-message" role="alert">{{ error }}</p>
    <div class="dialog-actions">
      <button type="button" class="button button-outline" :disabled="saving" @click="emit('cancel')">取消</button>
      <button type="button" class="button button-primary" :disabled="!canSave" @click="submit">
        {{ saving ? '保存中' : '保存新版本' }}
      </button>
    </div>

    <DomainDescriptionDialog
      v-if="descriptionOpen"
      :initial-text="savedDescription ?? ''"
      @save="onDescriptionSaved"
      @cancel="descriptionOpen = false"
    />

    <!-- 发布配置文件编辑（工作区定位；保存后通知父组件复查校验） -->
    <DomainConfigDialog v-if="domainOpen" :workspace-id="workspaceId || ''" @close="onConfigSaved" />
    <RelEditDialog
      v-if="relOpen"
      :workspace-id="workspaceId || ''"
      @close="closeConfigDialogs"
      @saved="onConfigSaved"
    />
    <DimValueConfigDialog
      v-if="dimOpen"
      :workspace-id="workspaceId || ''"
      :db-name="dbName || ''"
      @close="closeConfigDialogs"
      @saved="onConfigSaved"
    />
  </div>
</template>

<style scoped>
.jc-editor {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.jc-editor-actions {
  justify-content: flex-end;
}
.jc-change-note {
  margin: 0;
}
.jc-change-note > span:first-child {
  display: block;
  color: #2c3e58;
  font-weight: 700;
  font-size: 12px;
  margin-bottom: 6px;
}
.jc-change-note input {
  width: 100%;
  border: 1px solid #d2e0f2;
  border-radius: 10px;
  background: #f1f7ff;
  color: #20364f;
  padding: 10px 12px;
  outline: none;
}
.jc-change-note input:focus {
  border-color: #3a83dc;
  box-shadow: 0 0 0 3px #dbeeff;
}
/* 发布校验（与新建发布页同款标记） */
.jc-check-summary {
  display: inline-flex;
  align-items: center;
  padding: 7px 2px 7px 0;
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}
.jc-check-summary.ok {
  color: #16a34a;
}
.jc-check-summary.bad {
  color: #dc2626;
}
.jc-check-mark {
  display: inline-flex;
  flex-shrink: 0;
}
.jc-check-mark svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.jc-check-mark.ok {
  color: #16a34a;
}
.jc-check-mark.bad {
  color: #dc2626;
}
.jc-step-reason {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-style: normal;
  color: #dc4a4a;
  font-size: 11px;
}
</style>
