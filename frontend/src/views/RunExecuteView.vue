<script setup lang="ts">
/**
 * 执行页（doc 6.3 §4 编号 29/31）：`.../jobs/:jobId/run?revision_no=<n>&mode=default|custom`。
 *
 * - 从 query 读取并展示 revision_no 与执行模式；提交固定使用页面展示的版本，
 *   不自动切换最新版本（doc 6.2 §6.2）。地址栏携带的 revision_no 不能对应到
 *   该作业任何版本（含非法值）时停在错误态：不回退、不提交、不改地址栏；
 *   键完全不存在时才使用最新版本。
 * - default：展示该版本完整步骤与默认参数，不允许临时修改；
 *   custom：允许非空子集（可等于全集）与允许范围内的临时参数，临时参数不写回作业。
 * - 一次提交整组步骤 = 一个运行；步骤排序与校验由后端完成，前端不补选上游。
 * - 发布页四卡片（2026-10-26 定案，设计稿 35 流程）：作业信息 → 本次执行步骤 →
 *   执行确认 → 运行结果；「步骤校验」按钮在执行确认卡，校验状态标在步骤行，
 *   通过前「执行发布」保持灰色；custom 勾选/编辑仅限校验通过的步骤。
 *   正式请求仍由后端在创建运行前集中预检本次所选步骤，页面校验不能替代。
 * - 运行创建成功后保留原 query 并追加 run_id；刷新恢复查询同一运行，
 *   实际版本、步骤和参数以运行详情为准；查询失败只重试查询，不重新提交。
 * - 采集不增加发布式创建前集中预检（doc 6.2 §6.3）。
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '../api/http'
import type {
  JobItem,
  JobKind,
  PublishCheckResponse,
  PublishCheckStep,
  PublishPrecheckFailure,
  RevisionItem,
} from '../api/types'
import {
  createRun,
  getJob,
  getPublishCheck,
  listRevisions,
} from '../api/workspaces'
import DomainDescriptionDialog from '../components/DomainDescriptionDialog.vue'
import DimValueConfigDialog from '../components/DimValueConfigDialog.vue'
import DomainConfigDialog from '../components/DomainConfigDialog.vue'
import RelEditDialog from '../components/RelEditDialog.vue'
import RunPanel from '../components/RunPanel.vue'
import StepDetailDrawer from '../components/StepDetailDrawer.vue'
import StepGrid from '../components/StepGrid.vue'
import { useRunPolling } from '../composables/useRunPolling'
import { useWorkspaceContext } from '../composables/useWorkspaceContext'
import {
  PUBLISH_STEPS,
  executionOrder,
  unchosenDeps,
} from '../utils/steps'

const props = defineProps<{ kind: JobKind }>()

const route = useRoute()
const router = useRouter()

const workspaceId = computed(() =>
  typeof route.params.workspaceId === 'string' ? route.params.workspaceId : '',
)
const jobId = computed(() =>
  typeof route.params.jobId === 'string' ? route.params.jobId : '',
)
const kindText = computed(() => (props.kind === 'collection' ? '采集' : '发布'))
const jobBasePath = computed(
  () => `/workspaces/${workspaceId.value}/${props.kind === 'collection' ? 'collection' : 'publish'}`,
)

const ctx = useWorkspaceContext(() => workspaceId.value)

// ---- 作业与版本 ----

const job = ref<JobItem | null>(null)
const revisions = ref<RevisionItem[]>([])
const loading = ref(true)
const loadError = ref('')

/** 地址栏 revision_no 的原始文本；键不存在为 null（与空串/非法值区分） */
const queryRevisionRaw = computed(() => {
  const value = route.query.revision_no
  if (value === undefined) return null
  return Array.isArray(value) ? String(value[0] ?? '') : String(value)
})
const queryRevisionNo = computed(() => {
  const raw = queryRevisionRaw.value
  if (raw === null) return null
  const parsed = Number(raw)
  return Number.isInteger(parsed) && parsed >= 1 ? parsed : null
})
const mode = computed<'default' | 'custom'>(() => {
  const value = Array.isArray(route.query.mode) ? route.query.mode[0] : route.query.mode
  return value === 'custom' ? 'custom' : 'default'
})

/** 「修改步骤或参数」勾选框 ⇔ mode query：保持深链、刷新恢复与前进后退行为不变 */
function setMode(next: 'default' | 'custom') {
  if (next === mode.value) return
  void router.replace({ query: { ...route.query, mode: next } })
}

const revision = computed(() => {
  if (queryRevisionNo.value !== null) {
    const found = revisions.value.find((row) => row.revision_no === queryRevisionNo.value)
    if (found) return found
  }
  // 仅当地址栏完全没有 revision_no 键时才使用最新版本；
  // 键存在（99/0/abc/空串等）但对不上任何版本 → 不回退，停在错误态
  if (queryRevisionRaw.value === null) {
    return revisions.value.find((row) => row.revision_no === job.value?.latest_revision_no) ?? null
  }
  return null
})
const revisionNo = computed(() => revision.value?.revision_no ?? null)
const revisionSteps = computed(() => revision.value?.steps ?? [])
/** 地址栏版本不能对应到该作业任何版本 → 错误态：不回退、不提交、不改地址栏 */
const revisionInvalid = computed(
  () => queryRevisionRaw.value !== null && revision.value === null,
)

/** 作业存在更新版本时提示确认，不静默切换（doc 6.3 §4.2） */
const hasNewerRevision = computed(
  () =>
    job.value !== null &&
    revisionNo.value !== null &&
    (job.value.latest_revision_no ?? 0) > revisionNo.value,
)

const revisionDomainDescription = computed(() => {
  const domain = revision.value?.step_config?.domain as Record<string, unknown> | undefined
  return typeof domain?.description === 'string' ? domain.description : ''
})

async function load() {
  const id = jobId.value
  if (!id) return
  loading.value = true
  loadError.value = ''
  job.value = null
  revisions.value = []
  try {
    const [jobRow, revisionRows] = await Promise.all([getJob(props.kind, id), listRevisions(props.kind, id)])
    if (jobRow.workspace_id !== workspaceId.value) {
      await router.replace({ path: jobBasePath.value, query: { notice: 'not_found' } })
      return
    }
    job.value = jobRow
    revisions.value = revisionRows.items
  } catch (cause) {
    if (
      cause instanceof ApiError &&
      (cause.code === 'not_found' || cause.code === 'invalid_request')
    ) {
      await router.replace({ path: jobBasePath.value, query: { notice: 'not_found' } })
      return
    }
    loadError.value = cause instanceof Error ? cause.message : '作业信息加载失败。'
  } finally {
    loading.value = false
  }
}

watch(jobId, () => void load(), { immediate: true })

// ---- 步骤选择（custom 模式） ----

const selected = ref<Record<string, boolean>>({})
const tempDescription = ref<string | null>(null)
const tempDescriptionOpen = ref(false)

function resetSelection() {
  selected.value = Object.fromEntries(revisionSteps.value.map((step) => [step, true]))
  tempDescription.value = null
}

watch(revisionSteps, () => resetSelection(), { immediate: true })
watch(mode, () => {
  // 切回默认模式不保留手工草稿含义；再切回 custom 重新按版本全集初始化
  if (mode.value === 'default') resetSelection()
})

const chosenSteps = computed(() =>
  Object.keys(selected.value).filter((step) => selected.value[step]),
)
const depHints = computed(() =>
  props.kind === 'collection' ? unchosenDeps(chosenSteps.value) : [],
)
/** 交互提示：预计执行顺序（后端排序为准，前端只作提示） */
const previewOrder = computed(() =>
  props.kind === 'collection'
    ? executionOrder(chosenSteps.value)
    : PUBLISH_STEPS.filter((step) => chosenSteps.value.includes(step)),
)

function toggleStep(step: string) {
  // 发布 custom：只有通过发布校验的步骤可勾选（与新建页/详情页编辑器一致）
  if (props.kind === 'publish' && stepCheck(step)?.ready !== true) return
  selected.value = { ...selected.value, [step]: !selected.value[step] }
}

/** custom 全选：发布只选「版本允许 ∩ 校验通过」的步骤，采集为版本全集 */
function selectAllAvailable() {
  const steps =
    props.kind === 'publish'
      ? revisionSteps.value.filter((step) => stepCheck(step)?.ready === true)
      : revisionSteps.value
  selected.value = Object.fromEntries(steps.map((step) => [step, true]))
}

function onTempDescriptionSaved(text: string) {
  tempDescription.value = text
  tempDescriptionOpen.value = false
}

// ---- 发布校验（设计稿 35 流程：四卡片，校验按钮在执行确认卡） ----
// 结果反映点击时刻状态；执行发布要求本次所选步骤全部通过，后端仍会集中预检。

const check = ref<PublishCheckResponse | null>(null)
const checkLoading = ref(false)
const checkError = ref('')

const checkRan = computed(() => check.value !== null)
const checkByStep = computed(() => {
  const map = new Map<string, PublishCheckStep>()
  for (const step of check.value?.steps ?? []) map.set(step.step_name, step)
  return map
})

function stepCheck(step: string): PublishCheckStep | undefined {
  return checkByStep.value.get(step)
}

/** custom 可勾选白名单：发布 = 版本步骤 ∩ 校验通过；采集 = 版本步骤 */
const selectableSteps = computed(() =>
  props.kind === 'publish'
    ? revisionSteps.value.filter((step) => stepCheck(step)?.ready === true)
    : revisionSteps.value,
)

/** 确认区域展示的步骤：默认模式 = 版本步骤；custom = 预计执行顺序 */
const confirmSteps = computed(() =>
  mode.value === 'custom' ? previewOrder.value : revisionSteps.value,
)

/** 摘要只统计本次执行范围内的步骤（default=版本步骤，custom=所选子集），
    不把范围外的失败步骤计入「未通过」 */
const failedCount = computed(
  () => confirmSteps.value.filter((step) => stepCheck(step)?.ready !== true).length,
)
const passedCount = computed(
  () => confirmSteps.value.filter((step) => stepCheck(step)?.ready === true).length,
)

/** 执行发布门控：已校验且本次所选步骤全部通过 */
const checkPassedForRun = computed(() => {
  if (props.kind !== 'publish' || !checkRan.value) return false
  return confirmSteps.value.every((step) => stepCheck(step)?.ready === true)
})

async function loadCheck(): Promise<boolean> {
  if (props.kind !== 'publish' || !workspaceId.value) return false
  checkLoading.value = true
  checkError.value = ''
  try {
    check.value = await getPublishCheck(workspaceId.value)
    return true
  } catch (cause) {
    checkError.value = cause instanceof ApiError ? cause.message : '发布校验失败。'
    return false
  } finally {
    checkLoading.value = false
  }
}

// 校验结果更新后剔除已不通过的勾选（如上游产物在编辑期间变化）
watch(checkByStep, () => {
  if (props.kind !== 'publish' || mode.value !== 'custom') return
  const next: Record<string, boolean> = {}
  for (const [step, on] of Object.entries(selected.value)) {
    next[step] = on && stepCheck(step)?.ready === true
  }
  selected.value = next
})

// ---- Domain/Rel/Dim 编辑（doc 6.3 §4.1：发布执行页的能力） ----
// 编辑成功只刷新预检：不新建作业版本、不创建采集运行、不更新采集 marker 时间
const domainOpen = ref(false)
const relOpen = ref(false)
const dimOpen = ref(false)

function onConfigSaved() {
  domainOpen.value = false
  relOpen.value = false
  dimOpen.value = false
  void loadCheck()
}

// DomainConfigDialog 仅暴露 close（保存成功后自行关闭）；关闭后统一刷新预检
function onDomainDialogClose() {
  domainOpen.value = false
  void loadCheck()
}

// ---- 提交执行（doc 6.2 §6.2 / §6.3） ----

const executing = ref(false)
const execError = ref('')
const precheckFailures = ref<PublishPrecheckFailure[]>([])
const activeRun = useRunPolling(props.kind)
const stepDrawerStep = ref('')

const canExecute = computed(() => {
  if (
    ctx.sourceDisabled.value ||
    executing.value ||
    loading.value ||
    revisionInvalid.value ||
    revisionNo.value === null
  ) {
    return false
  }
  if (mode.value === 'custom' && chosenSteps.value.length === 0) return false
  // 发布：步骤校验通过前执行发布保持灰色（doc 6.3 §4.1；后端仍集中预检）
  if (props.kind === 'publish' && !checkPassedForRun.value) return false
  return true
})

function parsePrecheckFailures(error: unknown): PublishPrecheckFailure[] {
  if (error instanceof ApiError && Array.isArray(error.details)) {
    const rows = error.details as unknown[]
    return rows.filter(
      (row): row is PublishPrecheckFailure =>
        typeof row === 'object' &&
        row !== null &&
        typeof (row as PublishPrecheckFailure).step === 'string' &&
        typeof (row as PublishPrecheckFailure).reason === 'string',
    )
  }
  return []
}

async function execute() {
  // 地址栏版本无效时不得按其他版本提交（不回退到最新版本）
  if (revisionInvalid.value) return
  if (!canExecute.value || revisionNo.value === null) return
  executing.value = true
  execError.value = ''
  precheckFailures.value = []
  try {
    // default：省略 steps/step_config（= 不覆盖，执行该版本全部步骤与默认参数）
    // custom：非空子集 + 允许的临时参数；临时参数不写回作业
    const steps = mode.value === 'custom' ? chosenSteps.value : undefined
    const useTemp =
      props.kind === 'collection' &&
      mode.value === 'custom' &&
      steps !== undefined &&
      steps.includes('domain') &&
      tempDescription.value !== null
    const payload = {
      revision_no: revisionNo.value,
      mode: mode.value,
      ...(mode.value === 'custom'
        ? {
            steps,
            step_config: useTemp
              ? { domain: { description: tempDescription.value! } }
              : null,
          }
        : {}),
    }
    const response = await createRun(props.kind, jobId.value, payload)
    const runId = response.collection_run_id ?? response.publish_run_id ?? ''
    if (runId) {
      // 保留 revision_no/mode 等已有 query，仅追加/更新 run_id（doc 6.3 §4.4）
      await router.replace({ query: { ...route.query, run_id: runId } })
      activeRun.open(runId)
      stepDrawerStep.value = ''
    }
  } catch (cause) {
    precheckFailures.value = parsePrecheckFailures(cause)
    execError.value = cause instanceof ApiError ? cause.message : `${kindText.value}运行创建失败。`
  } finally {
    executing.value = false
  }
}

// ---- 运行恢复：URL 中的 run_id 即查询目标 ----

watch(
  () => (typeof route.query.run_id === 'string' ? route.query.run_id : ''),
  (runId) => {
    if (runId && runId !== activeRun.runId.value) activeRun.open(runId)
    if (!runId && activeRun.runId.value) activeRun.close()
  },
  { immediate: true },
)

function openStep(step: string) {
  stepDrawerStep.value = step
}

function switchStep(step: string) {
  stepDrawerStep.value = step
}
</script>

<template>
  <div class="rx-page">
    <header class="ds-jobs-header">
      <div>
        <span class="ds-kicker">{{ kind === 'collection' ? 'COLLECTION EXECUTION' : 'PUBLISH EXECUTION' }}</span>
        <h1>执行{{ kindText }}作业</h1>
        <p>
          工作区 {{ ctx.workspace.value?.workspace_name || '…' }}（{{ workspaceId }}）；
          按版本 {{ revisionNo ?? '—' }} 的配置发起，一次执行生成一个新的运行。
        </p>
      </div>
      <div class="rx-actions">
        <RouterLink class="ds-jobs-back" :to="`${jobBasePath}/jobs/${jobId}`">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
          返回
        </RouterLink>
      </div>
    </header>

    <p v-if="loading" class="ds-jobs-state">正在读取作业…</p>
    <div v-else-if="loadError" class="state-block error-state" role="alert">
      <p>{{ loadError }}</p>
      <button type="button" class="button button-outline" @click="load">重试</button>
    </div>

    <template v-else-if="job">
      <p v-if="ctx.sourceDisabled.value" class="ds-banner" role="alert">
        该工作区所属数据源已禁用，不能发起新的{{ kindText }}运行。
      </p>
      <p v-if="revisionInvalid" class="ds-banner" role="alert">
        版本 {{ queryRevisionRaw }} 不存在，不能按该版本执行。请修正地址中的
        revision_no，或从作业详情重新进入；本次不会改用其他版本提交。
      </p>
      <p v-else-if="hasNewerRevision" class="ds-banner" role="alert">
        作业已保存更新的版本 {{ job.latest_revision_no }}；本次执行仍使用页面展示的版本
        {{ revisionNo }}，提交前请确认。
        <RouterLink
          class="rx-reload"
          :to="{ query: { ...route.query, revision_no: String(job.latest_revision_no) } }"
        >
          改用版本 {{ job.latest_revision_no }}
        </RouterLink>
      </p>

      <!-- 作业信息 -->
      <section class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">JOB INFO</span>
            <h2>作业信息</h2>
          </div>
        </div>
        <div class="ds-jobs-facts">
          <p><span>作业名称</span><strong>{{ job.job_name }}</strong></p>
          <p><span>作业 ID</span><strong>{{ job.job_id }}</strong></p>
          <p>
            <span>执行版本/最新版本</span>
            <strong>{{ revisionNo ?? '—' }} / {{ job.latest_revision_no ?? '—' }}</strong>
          </p>
          <p><span>工作区名称</span><strong>{{ ctx.workspace.value?.workspace_name || '—' }}</strong></p>
          <p><span>数据源名称</span><strong>{{ ctx.source.value?.display_name || '—' }}</strong></p>
          <p><span>数据库名称</span><strong>{{ ctx.source.value?.db_name || '—' }}</strong></p>
        </div>
        <p v-if="revisionDomainDescription" class="rx-note">
          版本 {{ revisionNo }} 的 domain 首次生成输入：{{ revisionDomainDescription }}
        </p>
      </section>

      <!-- 步骤选择 -->
      <section v-if="!revisionInvalid" class="ds-jobs-card ds-new-job-steps">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">{{ kind === 'collection' ? 'COLLECTION STEPS' : 'PUBLISH STEPS' }}</span>
            <h2>本次执行步骤</h2>
            <p v-if="mode === 'default'">
              默认模式执行版本 {{ revisionNo ?? '—' }} 的全部步骤与默认参数，不允许临时修改；
              如需修改，勾选下方执行确认区的「修改步骤或参数」。
            </p>
            <p v-else>
              只能选择版本 {{ revisionNo ?? '—' }} 允许的步骤子集（可选全集）；
              临时参数只影响本次运行，不会保存回作业。
            </p>
          </div>
          <div v-if="mode === 'custom'" class="ds-new-job-step-actions">
            <button type="button" @click="selectAllAvailable">全选</button>
            <button type="button" @click="selected = {}">清空</button>
          </div>
        </div>

        <template v-if="mode === 'custom'">
          <p v-for="hint in depHints" :key="hint.step" class="ds-new-job-hint" role="status">
            {{ hint.step }} 未同选依赖步骤（{{ hint.deps.join('、') }}）：
            执行时将读取本工作区现有产物，不会自动补跑上游。
          </p>
        </template>
        <p v-if="kind === 'publish' && checkError" class="status-message" role="alert">{{ checkError }}</p>
        <p v-if="kind === 'publish'" class="ds-new-job-hint" role="status">
          发布步骤按固定顺序执行：db → ddl → json → domain → table_schema → dim_value → rel → sql。
        </p>

        <StepGrid
          :kind="kind"
          :selected="mode === 'default' ? Object.fromEntries(revisionSteps.map((s) => [s, true])) : selected"
          :available="mode === 'custom' ? selectableSteps : revisionSteps"
          :disabled="mode === 'default' || executing || checkLoading"
          @toggle="toggleStep"
        >
          <template #after-step="{ step }">
            <button
              v-if="kind === 'collection' && mode === 'custom' && step === 'domain'"
              type="button"
              class="nj-step-edit"
              :disabled="executing"
              @click="tempDescriptionOpen = true"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
              编辑
            </button>
            <!-- 发布：校验状态标记（default/custom 都显示，点击「步骤校验」后出现）；
                 custom 步骤行编辑入口写工作区配置文件而非临时参数（doc 6.3 §4.1），
                 需该步骤通过校验后可用。 -->
            <template v-if="kind === 'publish' && revisionSteps.includes(step)">
              <span
                v-if="checkRan && stepCheck(step)?.ready"
                class="rx-check-mark ok"
                title="发布校验通过"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="m8.4 12.2 2.4 2.4 4.8-5.2" /></svg>
              </span>
              <span
                v-else-if="checkRan"
                class="rx-check-mark bad"
                :title="`发布校验未通过：${stepCheck(step)?.reason ?? '原因未知'}`"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 7.6v5.2" /><path d="M12 16.4v.2" /></svg>
              </span>
              <em
                v-if="checkRan && !stepCheck(step)?.ready"
                class="rx-step-reason"
                :title="stepCheck(step)?.reason ?? '原因未知'"
              >
                {{ stepCheck(step)?.reason ?? '原因未知' }}
              </em>
              <template v-if="mode === 'custom'">
                <button
                  v-if="step === 'domain'"
                  type="button"
                  class="nj-step-edit"
                  :disabled="executing || checkLoading || stepCheck(step)?.ready !== true"
                  @click="domainOpen = true"
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
                  编辑
                </button>
                <button
                  v-if="step === 'rel'"
                  type="button"
                  class="nj-step-edit"
                  :disabled="executing || checkLoading || stepCheck(step)?.ready !== true"
                  @click="relOpen = true"
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
                  编辑
                </button>
                <button
                  v-if="step === 'dim_value'"
                  type="button"
                  class="nj-step-edit"
                  :disabled="executing || checkLoading || stepCheck(step)?.ready !== true"
                  @click="dimOpen = true"
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
                  编辑
                </button>
              </template>
            </template>
          </template>
        </StepGrid>
      </section>

      <!-- 执行确认（地址栏版本无效时不展示，按钮不可提交） -->
      <section v-if="!revisionInvalid" class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">CONFIRM</span>
            <h2>执行确认</h2>
          </div>
          <div class="rx-confirm-actions">
            <template v-if="kind === 'publish'">
              <span
                v-if="checkRan"
                class="rx-check-summary"
                :class="failedCount > 0 ? 'bad' : 'ok'"
              >
                {{ failedCount > 0 ? `${failedCount} 项未通过` : `${passedCount} 项通过` }}
              </span>
              <button
                type="button"
                class="button button-outline rx-check-btn"
                :disabled="checkLoading || executing"
                @click="loadCheck"
              >
                {{ checkLoading ? '正在校验' : '步骤校验' }}
              </button>
            </template>
            <label class="rx-custom-toggle">
              <input
                type="checkbox"
                :checked="mode === 'custom'"
                :disabled="executing"
                @change="setMode(($event.target as HTMLInputElement).checked ? 'custom' : 'default')"
              />
              修改步骤或参数
            </label>
            <button type="button" class="button button-primary" :disabled="!canExecute" @click="execute">
              {{ executing ? '创建中' : kind === 'publish' ? '执行发布' : `执行${kindText}运行` }}
            </button>
          </div>
        </div>
        <div class="rx-confirm">
          <div class="rx-confirm-row">
            <p>
              <span>执行版本</span><strong>{{ revisionNo ?? '—' }}</strong>
            </p>
            <p>
              <span>执行模式</span>
              <strong>{{ mode === 'default' ? '按默认配置，手工执行。' : '自定义步骤或参数，手工执行。' }}</strong>
            </p>
          </div>
          <p class="rx-confirm-steps-line">
            <span>本次执行步骤</span>
            <strong v-if="confirmSteps.length === 0" class="rx-confirm-empty">
              {{ mode === 'default' ? '—' : '尚未选择' }}
            </strong>
            <strong v-else class="rx-confirm-steps">
              <template v-for="(step, index) in confirmSteps" :key="step">
                <span v-if="index > 0" class="rx-step-arrow" aria-hidden="true">→</span>
                <span class="rx-step-chip">{{ step }}</span>
              </template>
            </strong>
          </p>
        </div>
        <p v-if="execError" class="ds-jobs-state error">{{ execError }}</p>
        <div v-if="precheckFailures.length" class="pc-failures" role="alert">
          <p>发布预检未通过，本次请求未创建发布运行：</p>
          <ul>
            <li v-for="failure in precheckFailures" :key="failure.step + failure.reason">
              <strong>{{ failure.step }}</strong>：{{ failure.reason }}
            </li>
          </ul>
        </div>
      </section>

      <!-- 运行结果（run_id 恢复或刚创建） -->
      <section v-if="activeRun.runId.value" class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">RUN RESULT</span>
            <h2>运行结果</h2>
          </div>
        </div>
        <RunPanel
          :kind="kind"
          :run="activeRun.detail.value"
          :loading="activeRun.loading.value"
          :error="activeRun.error.value"
          @retry="activeRun.retry()"
          @open-step="openStep"
        />
      </section>
    </template>

    <DomainDescriptionDialog
      v-if="tempDescriptionOpen"
      :initial-text="tempDescription ?? revisionDomainDescription"
      note="仅本次运行生效，不写回作业版本；只在 db_domains.yaml 不存在时参与首次生成，不覆盖已有描述。"
      @save="onTempDescriptionSaved"
      @cancel="tempDescriptionOpen = false"
    />

    <!-- 发布前编辑入口（工作区定位；doc 6.3 §4.1，与作业详情页同款） -->
    <DomainConfigDialog
      v-if="domainOpen"
      :workspace-id="workspaceId"
      @close="onDomainDialogClose"
    />
    <RelEditDialog
      v-if="relOpen"
      :workspace-id="workspaceId"
      @close="relOpen = false"
      @saved="onConfigSaved"
    />
    <DimValueConfigDialog
      v-if="dimOpen"
      :workspace-id="workspaceId"
      :db-name="ctx.source.value?.db_name || ''"
      @close="dimOpen = false"
      @saved="onConfigSaved"
    />

    <StepDetailDrawer
      v-if="stepDrawerStep"
      :kind="kind"
      :run="activeRun.detail.value"
      :step-name="stepDrawerStep"
      :loading="activeRun.loading.value"
      :error="activeRun.error.value"
      @close="stepDrawerStep = ''"
      @select-step="switchStep"
    />
  </div>
</template>

<style scoped>
.rx-page {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.rx-actions {
  display: flex;
  gap: 10px;
}
.rx-confirm-actions {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-shrink: 0;
}
.rx-custom-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #2c3e58;
  cursor: pointer;
  user-select: none;
}
.rx-custom-toggle input {
  accent-color: #327dd3;
  width: 15px;
  height: 15px;
  margin: 0;
}
.rx-note {
  margin: 0;
  color: #66788f;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.rx-reload {
  color: #2e7cd6;
  font-weight: 700;
  text-decoration: underline;
  margin-left: 6px;
}
.rx-confirm {
  display: flex;
  flex-direction: column;
  border: 1px solid #e1e9f4;
  border-radius: 12px;
  background: #fcfdff;
  overflow: hidden;
  font-size: 12px;
}
.rx-confirm-row,
.rx-confirm-steps-line {
  display: flex;
  align-items: center;
  gap: 28px;
  min-width: 0;
  margin: 0;
  padding: 12px 16px;
}
.rx-confirm-steps-line {
  gap: 12px;
  border-top: 1px solid #e1e9f4;
}
.rx-confirm-row p + p {
  padding-left: 28px;
  border-left: 1px solid #d9e3ef;
}
.rx-confirm p {
  margin: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  white-space: nowrap;
  font-size: 12px;
}
.rx-confirm p > span,
.rx-confirm strong,
.rx-step-chip,
.rx-step-arrow {
  font-size: 12px;
  line-height: 1.4;
}
.rx-confirm p > span {
  flex-shrink: 0;
  color: #66788f;
  font-weight: 400;
}
.rx-confirm strong {
  font-weight: 600;
  color: #17293f;
  white-space: nowrap;
}
.rx-confirm-empty {
  color: #8aa0b8;
  font-weight: 600;
}
.rx-confirm-steps {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  overflow: hidden;
  font-weight: 600;
}
.rx-step-chip {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 8px;
  border-radius: 6px;
  background: #eef3f9;
  color: #17293f;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 12px;
  font-weight: 500;
}
.rx-step-arrow {
  color: #8aa0b8;
  font-size: 12px;
  font-weight: 400;
}
/* 发布校验（与新建页/详情页同款标记；按钮在执行确认卡） */
.rx-check-btn {
  min-height: 34px;
  font-size: 12px;
}
.rx-check-summary {
  display: inline-flex;
  align-items: center;
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}
.rx-check-summary.ok {
  color: #16a34a;
}
.rx-check-summary.bad {
  color: #dc2626;
}
.rx-check-mark {
  display: inline-flex;
  flex-shrink: 0;
}
.rx-check-mark svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.rx-check-mark.ok {
  color: #16a34a;
}
.rx-check-mark.bad {
  color: #dc2626;
}
.rx-step-reason {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-style: normal;
  color: #dc4a4a;
  font-size: 11px;
}
.pc-failures {
  border: 1px solid #efcaca;
  background: #fbecec;
  border-radius: 12px;
  padding: 12px 16px;
  color: #c64f4f;
}
.pc-failures p {
  margin: 0 0 6px;
  font-weight: 700;
}
.pc-failures ul {
  margin: 0;
  padding-left: 18px;
}
.pc-failures li {
  font-size: 12px;
  overflow-wrap: anywhere;
}
</style>
