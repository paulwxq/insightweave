<script setup lang="ts">
/**
 * 保存新版本编辑器（doc 6.2 §9.1.4；doc 7.2 §3.2/§4 改造）：编辑执行配置 →
 * 新增不可变版本；旧版本不可原地更新。采集版本可保存 domain.description 默认输入。
 * 发布校验可手动触发，保存时由父组件自动复查；配置完整即可发起保存，
 * 不以尚未执行的手动校验结果禁用保存按钮。
 *
 * dim_value/domain 编辑的是版本草稿（doc 7.2 §3.2）：弹窗从当前草稿经 props
 * 初始化、确认只回传清单，不写工作区配置文件；保存时完整清单随作业写入新版本
 * 默认配置（完整替换语义，不做增量合并）。勾选 dim_value/domain 时对应草稿
 * 必须完整才能保存（dim：至少一张表且每表至少一个字段；domain：至少一个域）。
 *
 * 版本无对应配置（草稿为 null）时自动读取 YAML 初始化（doc 7.2 §3.2，
 * 非运行时回退）：字符串形态转单元素数组、含逗号字符串转空数组并提示改用
 * 字段列表；初始化失败可手工配置，不重复初始化覆盖用户草稿。
 * Rel 不再提供编辑入口（doc 7.2 §7.6，保留 rel 步骤选择与执行）。
 */
import { computed, ref, watch } from 'vue'
import type {
  DimValueTableParam,
  DomainParam,
  JobKind,
  PublishCheckStep,
} from '../api/types'
import DimValueConfigDialog from './DimValueConfigDialog.vue'
import DomainConfigDialog from './DomainConfigDialog.vue'
import DomainDescriptionDialog from './DomainDescriptionDialog.vue'
import { usePublishConfigInitialization } from '../composables/usePublishConfigInitialization'
import StepGrid from './StepGrid.vue'
import { allSteps, unchosenDeps } from '../utils/steps'
import {
  parseDimTables,
  parseDomains,
} from '../utils/publishStepConfig'

const props = defineProps<{
  kind: JobKind
  /** 编辑起点（通常为当前查看版本） */
  initialSteps: string[]
  initialStepConfig: Record<string, unknown> | null
  saving: boolean
  error: string
  /** 发布：Domain/Dim 配置编辑弹窗的工作区定位 */
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

// ---- 版本草稿（dim_value/domain；null = 版本无该配置，无版本参数时自动读取 YAML，失败可手工配置） ----

const dimDraft = ref<DimValueTableParam[] | null>(null)
const domainDraft = ref<DomainParam[] | null>(null)

function syncDraftsFromProps() {
  if (props.kind !== 'publish') {
    dimDraft.value = null
    domainDraft.value = null
    return
  }
  dimDraft.value = parseDimTables(props.initialStepConfig)
  domainDraft.value = parseDomains(props.initialStepConfig)
}
syncDraftsFromProps()
watch(() => [props.initialStepConfig, props.kind], syncDraftsFromProps)

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

/** 版本保存完整性（doc 7.2 §5.1）：勾选 dim_value/domain 必须携带完整清单 */
const dimIncomplete = computed(
  () =>
    props.kind === 'publish' &&
    chosenSteps.value.includes('dim_value') &&
    (!dimDraft.value ||
      dimDraft.value.length === 0 ||
      dimDraft.value.some((table) => table.embedding_col.length === 0)),
)
const domainIncomplete = computed(
  () =>
    props.kind === 'publish' &&
    chosenSteps.value.includes('domain') &&
    (!domainDraft.value || domainDraft.value.length === 0),
)

const canSave = computed(() => {
  if (props.saving || props.checking || initializing.value || chosenSteps.value.length === 0) return false
  if (props.kind !== 'publish') return true
  if (dimIncomplete.value || domainIncomplete.value) return false
  // 父组件 saveRevision 会自动发布校验并显示具体失败原因。
  return true
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

// ---- 版本草稿编辑弹窗与自动初始化（doc 7.2 §3.2） ----

const domainOpen = ref(false)
const dimOpen = ref(false)

function onDimApply(tables: DimValueTableParam[]) {
  dimDraft.value = tables
  dimOpen.value = false
}

function onDomainApply(domains: DomainParam[]) {
  domainDraft.value = domains
  domainOpen.value = false
}

const { initializing, initError } = usePublishConfigInitialization(
  () => [props.kind, props.workspaceId, props.initialStepConfig] as const,
  dimDraft,
  domainDraft,
)

function submit() {
  if (!canSave.value) return
  const steps = chosenSteps.value
  const stepConfig: Record<string, Record<string, unknown>> = {}
  if (props.kind === 'collection') {
    if (steps.includes('domain') && savedDescription.value !== null) {
      stepConfig.domain = { description: savedDescription.value }
    }
  } else {
    // 完整清单随保存写入新版本（完整替换语义；不做增量合并）
    if (steps.includes('dim_value') && dimDraft.value) {
      stepConfig.dim_value = { tables: dimDraft.value }
    }
    if (steps.includes('domain') && domainDraft.value) {
      stepConfig.domain = { domains: domainDraft.value }
    }
  }
  emit('save', {
    steps,
    step_config: Object.keys(stepConfig).length > 0 ? stepConfig : null,
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
      :disabled="saving || checking || initializing"
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
          <!-- 版本草稿编辑（doc 7.2 §3.2）：只改将保存进新版本的清单，
               不写工作区配置文件；rel 无编辑入口 -->
          <template v-if="step === 'domain'">
            <em v-if="domainDraft !== null" class="jc-draft-mark">
              已配置 {{ domainDraft.length }} 个域
            </em>
            <button
              type="button"
              class="nj-step-edit"
              :disabled="saving || checking || initializing"
              @click="domainOpen = true"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
              编辑
            </button>
          </template>
          <template v-if="step === 'dim_value'">
            <em v-if="dimDraft !== null" class="jc-draft-mark">
              已配置 {{ dimDraft.length }} 张表
            </em>
            <button
              type="button"
              class="nj-step-edit"
              :disabled="saving || checking || initializing"
              @click="dimOpen = true"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
              编辑
            </button>
          </template>
        </template>
      </template>
    </StepGrid>

    <p v-if="initError" class="status-message" role="alert">{{ initError }}</p>
    <p v-if="dimIncomplete" class="status-message" role="alert">
      dim_value 需要完整的版本配置：请为至少一张表指定值向量化字段（自动读取配置后编辑），
      或取消勾选 dim_value。
    </p>
    <p v-if="domainIncomplete" class="status-message" role="alert">
      domain 需要完整的版本配置：请配置至少一个业务域（自动读取配置后编辑），
      或取消勾选 domain。
    </p>

    <label class="field jc-change-note">
      <span>变更说明（可选）</span>
      <input v-model="changeNote" type="text" placeholder="本次配置变更的原因" :disabled="saving" />
    </label>

    <p v-if="error" class="status-message" role="alert">{{ error }}</p>
    <p v-if="kind === 'publish'" class="jc-save-hint">
      保存时会自动进行发布校验；也可点击上方“发布校验”提前检查。
    </p>
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

    <!-- 版本草稿编辑（doc 7.2 §3.2：确认只回传清单，随保存写入新版本） -->
    <DomainConfigDialog
      v-if="domainOpen"
      :workspace-id="workspaceId || ''"
      :db-name="dbName || ''"
      :initial-domains="domainDraft ?? []"
      purpose="version"
      @close="domainOpen = false"
      @apply="onDomainApply"
    />
    <DimValueConfigDialog
      v-if="dimOpen"
      :workspace-id="workspaceId || ''"
      :db-name="dbName || ''"
      :initial-tables="dimDraft ?? []"
      purpose="version"
      @close="dimOpen = false"
      @apply="onDimApply"
    />
  </div>
</template>

<style scoped>
.jc-save-hint {
  color: #66788f;
  font-size: 12px;
  margin: 8px 0;
}
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
/* 版本草稿状态与自动初始化（doc 7.2 §3.2） */
.jc-draft-mark {
  font-style: normal;
  font-size: 9px;
  font-weight: 400;
  padding: 1px 6px;
  border-radius: 6px;
  background: #e3eefb;
  border: 1px solid #c5d7ee;
  color: #2e7cd6;
  white-space: nowrap;
}

</style>
