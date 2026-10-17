<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { createCollectionJob, getDataSource, getSqlReadiness, listCollectionJobs } from '../api/dataSources'
import { displayDate } from '../api/format'
import { ApiError } from '../api/http'
import DomainDescriptionDialog from '../components/DomainDescriptionDialog.vue'
import type { CollectionJobStepResult, DataSourceListItem, SqlReadinessResponse } from '../api/types'
import { executionOrder, upstreamBlockedMessage } from '../utils/collectionStepOrder'

interface JobStep {
  id: string
  label: string
  enabled: boolean
}

interface StepGroup {
  id: string
  title: string
  chip: string
  steps: JobStep[]
}

const STEP_GROUPS: StepGroup[] = [
  {
    id: 'a',
    title: 'a.) 对于数据库整体',
    chip: 'a.) 数据库整体',
    steps: [{ id: 'db', label: 'db', enabled: true }],
  },
  {
    id: 'b',
    title: 'b.) 对表进行抽样画像',
    chip: 'b.) 抽样画像',
    steps: [
      { id: 'ddl', label: 'ddl', enabled: true },
      { id: 'json', label: 'json', enabled: true },
      { id: 'md', label: 'md', enabled: true },
      { id: 'domain', label: '--generate-domains --description "…"', enabled: false },
      { id: 'dim_config', label: '--generate-dim-config', enabled: false },
    ],
  },
  {
    id: 'c',
    title: 'c.) 分析表之间的关联关系',
    chip: 'c.) 关联关系',
    steps: [{ id: 'rel', label: 'rel', enabled: false }],
  },
  {
    id: 'd',
    title: 'd.) 生成 NL2SQL RAG metadata',
    chip: 'd.) RAG metadata',
    steps: [{ id: 'sql', label: 'sql', enabled: false }],
  },
]

const route = useRoute()
const router = useRouter()
const sourceId = computed(() =>
  typeof route.params.sourceId === 'string' ? route.params.sourceId : '',
)
const rerunJobId = computed(() =>
  typeof route.query.job_id === 'string' ? route.query.job_id : '',
)
const source = ref<DataSourceListItem | null>(null)
const loading = ref(true)
const loadError = ref('')
const selected = ref<Record<string, boolean>>({})
const running = ref(false)
const runningStepId = ref('')
const jobId = ref('')
const runError = ref('')
const runResults = ref<CollectionJobStepResult[]>([])
// doc 21.1 §8：重跑作业的步骤名集合与页面加载时的生效描述（基准值）；
// savedDescription/savedSync 是本次会话在弹窗中保存的草稿确认结果。
const rerunSteps = ref<Set<string>>(new Set())
const sqlReadiness = ref<SqlReadinessResponse | null>(null)
const sqlReadinessError = ref('')
const sqlReadinessPending = ref(false)
let readinessRequest = 0
const descriptionBaseline = ref('')
const savedDescription = ref<string | null>(null)
const savedSync = ref(false)
const descriptionDialogOpen = ref(false)

const dbTypeVersion = computed(() => {
  if (!source.value) return ''
  return source.value.db_version
    ? `${source.value.db_type} ${source.value.db_version}`
    : source.value.db_type
})
const isDisabled = computed(() => source.value !== null && !source.value.is_active)
const selectedChips = computed(() =>
  STEP_GROUPS.filter((group) => group.steps.some((step) => selected.value[step.id])).map(
    (group) => group.chip,
  ),
)
// doc 21.1 §8.2：domain 可勾选 = 本次选择含 md，或重跑作业 steps 含 md；
// doc 22.1 §8.2：dim_config 可勾选 = 本次选择含 json（新作业还要求同选 ddl），
// 或重跑作业 steps 含 json。可用性函数只对这两个条目生效，其余按静态 enabled。
const chosenIncludesMd = computed(() => Boolean(selected.value.md))
const domainAvailable = computed(
  () => chosenIncludesMd.value || (rerunJobId.value !== '' && rerunSteps.value.has('md')),
)
const dimConfigAvailable = computed(() => {
  if (rerunJobId.value !== '') {
    return Boolean(selected.value.json) || rerunSteps.value.has('json')
  }
  return Boolean(selected.value.ddl && selected.value.json)
})
// doc 23.1 §8.2：rel 可勾选 = json 依赖满足 且 domain 依赖满足；
// 每个依赖独立判定：本次勾选（本次产出）或重跑作业 steps 已含（读既有产物）
const relJsonSatisfied = computed(() =>
  rerunJobId.value !== ''
    ? Boolean(selected.value.json) || rerunSteps.value.has('json')
    : Boolean(selected.value.json),
)
const relDomainSatisfied = computed(() =>
  rerunJobId.value !== ''
    ? Boolean(selected.value.domain) || rerunSteps.value.has('domain')
    : Boolean(selected.value.domain),
)
const relAvailable = computed(
  () => relJsonSatisfied.value && relDomainSatisfied.value,
)
const sqlAvailable = computed(() => {
  if (!rerunJobId.value) {
    return ['ddl', 'json', 'md', 'domain', 'rel'].every((step) => Boolean(selected.value[step]))
  }
  if (sqlReadinessPending.value || !sqlReadiness.value || sqlReadinessError.value) return false
  return ['md', 'domain', 'rel'].every(
    (step) => Boolean(selected.value[step]) || !sqlReadiness.value!.missing.includes(step),
  )
})
function isStepEnabled(step: JobStep) {
  if (step.id === 'domain') return domainAvailable.value
  if (step.id === 'dim_config') return dimConfigAvailable.value
  if (step.id === 'rel') return relAvailable.value
  if (step.id === 'sql') return sqlAvailable.value
  return step.enabled
}
const chosenSteps = computed(() =>
  STEP_GROUPS.flatMap((group) => group.steps)
    .filter((step) => isStepEnabled(step) && selected.value[step.id])
    .map((step) => step.id),
)
// doc 18 §5：新建作业勾选 JSON 但未勾 DDL 时仅提示，不阻断——由后端
// 准备链统一报告"同一作业的 DDL 产物未就绪"；复用重跑时由后端校验。
const jsonWithoutDdlHint = computed(() => {
  if (rerunJobId.value) return ''
  const chosen = new Set(chosenSteps.value)
  return chosen.has('json') && !chosen.has('ddl')
    ? '当前作业没有 DDL 产物将无法生成 JSON，可先运行 DDL 或选择已有 DDL 成功的作业补跑。'
    : ''
})

// doc 20.1 §11.2：发布入口按整个作业的最新汇总状态决定，不再以本次 db/ddl 成功为条件；
// 汇总为 running 或 failed（含本次未执行的旧步骤）时不显示入口。
// 请求失败或取不到作业状态时 jobStatus 保持为空，同样不显示。
const jobStatus = ref<'' | 'success' | 'failed' | 'running'>('')
const canPublishNext = computed(() => Boolean(jobId.value) && jobStatus.value === 'success')

// doc 19.1 §6：勾选 MD 而未勾选 JSON 时提示同作业 JSON 依赖，不在前端
// 阻断；同选 DDL 时说明 MD 读取的是既有 JSON，本次 DDL 不会更新它。
const mdWithoutJsonHint = computed(() => {
  const chosen = new Set(chosenSteps.value)
  if (!chosen.has('md') || chosen.has('json')) return ''
  // doc 19.1 §6：勾选 MD 且未勾选 JSON 时均显示依赖提示；统一基调为
  // "MD 依赖同作业已成功的 JSON"，再按是否同选 DDL 补充说明。
  const ddlNote = chosen.has('ddl')
    ? '本次 DDL 不会更新 JSON；如需基于本次 DDL 生成 MD，请同时勾选 JSON。'
    : ''
  if (rerunJobId.value) {
    return 'MD 依赖同作业已成功的 JSON，将按既有 JSON 生成。' + ddlNote
  }
  return (
    'MD 依赖同作业已成功的 JSON；当前作业没有 JSON 产物将无法生成 MD，' +
    '可先运行 JSON 或选择已有 JSON 成功的作业补跑。' + ddlNote
  )
})

// doc 21.1 §8.2：domain 不可用提示（勾选条件不满足时显示在步骤区）
const domainHint = computed(() => {
  if (domainAvailable.value) return ''
  if (rerunJobId.value) return '该作业没有 MD 步骤，无法运行 Domain；请先执行 MD。'
  return 'Domain 依赖同作业已成功的 MD 产物；请同时勾选 MD，或从已有 MD 的作业重跑。'
})

// doc 21.1 §8.2：重跑模式勾选 domain 未勾 md、且同选 ddl/json 的使用流程提示（不阻断）
const domainReadsExistingMdHint = computed(() => {
  if (!rerunJobId.value) return ''
  const chosen = new Set(chosenSteps.value)
  if (!chosen.has('domain') || chosen.has('md')) return ''
  if (!chosen.has('ddl') && !chosen.has('json')) return ''
  return (
    'Domain 读取既有 MD 产物；本次 DDL/JSON 不会更新 MD——' +
    '如需基于本次 DDL/JSON 重新生成 MD，请同时勾选 MD。'
  )
})

// doc 22.1 §8.2：dim_config 不可用提示（勾选条件不满足时显示在步骤区）
const dimConfigHint = computed(() => {
  if (dimConfigAvailable.value) return ''
  if (rerunJobId.value) return '该作业没有 JSON 步骤，无法运行 Dim Config；请先执行 JSON。'
  return 'Dim Config 依赖同作业已成功的 JSON 产物；新建作业请同时勾选 DDL 与 JSON，或从已有 JSON 成功的作业重跑。'
})

// doc 22.1 §8.2：重跑模式勾选 dim_config 未勾 json、且同选 ddl 的使用流程提示（不阻断）
const dimConfigReadsExistingJsonHint = computed(() => {
  if (!rerunJobId.value) return ''
  const chosen = new Set(chosenSteps.value)
  if (!chosen.has('dim_config') || chosen.has('json')) return ''
  if (!chosen.has('ddl')) return ''
  return (
    'Dim Config 读取既有 JSON 产物；本次 DDL 不会更新 JSON——' +
    '如需基于本次 DDL 重新生成 JSON，请同时勾选 JSON。'
  )
})

// doc 23.1 §8.2：rel 不可用提示（勾选条件不满足时显示在步骤区）
const relHint = computed(() => {
  if (relAvailable.value) return ''
  if (rerunJobId.value) {
    return '该作业没有 JSON / Domain 步骤，无法运行 Rel；请先补跑缺失步骤。'
  }
  return (
    'Rel 依赖同作业已成功的 JSON 与 Domain 产物；请同时勾选 JSON 与 Domain' +
    '（并确保 DDL/MD 就绪），或从已有 JSON、Domain 成功的作业重跑。'
  )
})

// doc 23.1 §8.2：重跑模式的使用流程提示按依赖逐项生成（R2）——
// 只把未在本次勾选的依赖称为"既有产物"，后半句对着缺的依赖点名；
// 两项均勾选时不显示
const relReadsExistingHint = computed(() => {
  if (!rerunJobId.value) return ''
  const chosen = new Set(chosenSteps.value)
  if (!chosen.has('rel')) return ''
  const existing: string[] = []
  if (!chosen.has('json')) existing.push('JSON')
  if (!chosen.has('domain')) existing.push('Domain')
  if (existing.length === 0) return ''
  const tail =
    existing.length === 1
      ? `请同时勾选 ${existing[0]}。`
      : '请同时勾选对应步骤。'
  return `Rel 读取既有 ${existing.join(' / ')} 产物；如需基于本次采集重新生成，${tail}`
})

const sqlHint = computed(() => {
  if (sqlAvailable.value) return ''
  if (!rerunJobId.value) return 'SQL 样例依赖本次勾选 DDL、JSON、MD、Domain 和 Rel。'
  if (sqlReadinessPending.value) return '正在检查同作业 MD、Domain、Rel 产物。'
  return sqlReadinessError.value || sqlReadiness.value?.message || '正在检查同作业 MD、Domain、Rel 产物。'
})

async function refreshSqlReadiness() {
  if (!rerunJobId.value || !jobId.value) return
  const request = ++readinessRequest
  sqlReadinessPending.value = true
  sqlReadinessError.value = ''
  try {
    const response = await getSqlReadiness(sourceId.value, jobId.value)
    if (request === readinessRequest) sqlReadiness.value = response
  } catch (error) {
    if (request === readinessRequest) {
      sqlReadinessError.value = error instanceof ApiError ? error.message : 'SQL 输入就绪检查失败'
    }
  } finally {
    if (request === readinessRequest) sqlReadinessPending.value = false
  }
}

// doc 21.1 §8.3：链接文本只反映本次会话相对基准值（页面加载时生效描述）的变化；
// 判据是与基准值比较，重开弹窗不改文字直接保存仍显示前 6 字。
// 前 6 个字符按 Unicode 码点截取（与后端计数口径一致，且不会拆断代理对）。
const descriptionLinkText = computed(() => {
  const saved = savedDescription.value
  if (saved !== null && saved !== descriptionBaseline.value) {
    const prefix = [...saved].slice(0, 6).join('')
    return `--description "${prefix}…"`
  }
  return '--description "…"'
})

function onDescriptionSaved(text: string, sync: boolean) {
  savedDescription.value = text
  savedSync.value = sync
  descriptionDialogOpen.value = false
}

function openDescriptionDialog() {
  descriptionDialogOpen.value = true
}

const resultByStep = computed(() => {
  const rows = new Map<string, CollectionJobStepResult>()
  for (const row of runResults.value) rows.set(row.step, row)
  return rows
})
const resultGroups = computed(() =>
  STEP_GROUPS.map((group) => ({
    title: group.title,
    rows: group.steps.flatMap((step) => {
      const row = resultByStep.value.get(step.id)
      return row ? [{ ...row, label: step.label }] : []
    }),
  })).filter((group) => group.rows.length > 0),
)
const successCount = computed(
  () => runResults.value.filter((row) => row.status === 'success').length,
)
const failedCount = computed(
  () => runResults.value.filter((row) => row.status === 'failed').length,
)
const runningStepLabel = computed(() => {
  if (!runningStepId.value) return ''
  for (const group of STEP_GROUPS) {
    const step = group.steps.find((item) => item.id === runningStepId.value)
    if (step) return step.label
  }
  return runningStepId.value
})

function storedSelection(steps: string[]): Record<string, boolean> {
  const staticEnabled = new Set(
    STEP_GROUPS.flatMap((group) =>
      group.steps.filter((step) => step.enabled).map((step) => step.id),
    ),
  )
  const hasMd = steps.includes('md')
  const hasJson = steps.includes('json')
  const next: Record<string, boolean> = {}
  for (const step of steps) {
    if (step === 'domain') {
      // doc 21.1 §8.4：含 domain 的作业预勾选 domain（受可用性约束——重跑
      // 模式要求该作业 steps 含 md）
      if (hasMd) next.domain = true
      continue
    }
    if (step === 'dim_config') {
      // doc 22.1 §8.3：含 dim_config 的作业预勾选（受可用性约束——该作业
      // steps 含 json）
      if (hasJson) next.dim_config = true
      continue
    }
    if (step === 'rel') {
      // doc 23.1 §8.3：含 rel 的作业预勾选（受可用性约束——该作业
      // steps 同时含 json 与 domain）
      if (hasJson && steps.includes('domain')) next.rel = true
      continue
    }
    if (step === 'sql') continue
    if (staticEnabled.has(step)) next[step] = true
  }
  return next
}

function enabledSteps(group: StepGroup) {
  return group.steps.filter(isStepEnabled)
}

function groupOn(group: StepGroup) {
  const steps = enabledSteps(group)
  return steps.length > 0 && steps.every((step) => selected.value[step.id])
}

function toggleStep(step: JobStep) {
  if (!isStepEnabled(step)) return
  selected.value = { ...selected.value, [step.id]: !selected.value[step.id] }
}

function toggleGroup(group: StepGroup) {
  const steps = enabledSteps(group)
  if (steps.length === 0) return
  const next = !groupOn(group)
  const copy = { ...selected.value }
  for (const step of steps) copy[step.id] = next
  selected.value = copy
}

function selectAvailable(on: boolean) {
  // doc 21.1 §8.2：按点击瞬间的可用性执行——先快照可用项再赋值，避免
  // 勾选 md 后同一次点击顺带选中 domain；md 选中后再点一次会包含 domain。
  const enabledIds = STEP_GROUPS.flatMap((group) => group.steps)
    .filter((step) => isStepEnabled(step))
    .map((step) => step.id)
  const copy = { ...selected.value }
  for (const id of enabledIds) copy[id] = on
  selected.value = copy
}

// doc 21.1 §8.2：失去可用性时自动清除已勾选的 domain
watch(domainAvailable, (available) => {
  if (!available && selected.value.domain) {
    selected.value = { ...selected.value, domain: false }
  }
})

// doc 22.1 §8.2：失去可用性时自动清除已勾选的 dim_config
watch(dimConfigAvailable, (available) => {
  if (!available && selected.value.dim_config) {
    selected.value = { ...selected.value, dim_config: false }
  }
})

// doc 23.1 §8.2：失去可用性时自动清除已勾选的 rel
watch(relAvailable, (available) => {
  if (!available && selected.value.rel) {
    selected.value = { ...selected.value, rel: false }
  }
})

watch([sqlAvailable, sqlReadinessPending], ([available, pending]) => {
  if (!available && !pending && selected.value.sql) {
    selected.value = { ...selected.value, sql: false }
  }
})

watch(() => [selected.value.md, selected.value.domain, selected.value.rel], () => {
  if (rerunJobId.value && jobId.value && !running.value && !loading.value) void refreshSqlReadiness()
})

async function refreshJobSummary(activeJobId: string) {
  try {
    const jobs = await listCollectionJobs(sourceId.value)
    jobStatus.value =
      jobs.items.find((item) => item.job_id === activeJobId)?.job_status ?? ''
    if (source.value) {
      source.value = { ...source.value, description: jobs.source.description }
    }
  } catch {
    jobStatus.value = ''
  }
}

async function runJob() {
  if (running.value || sqlReadinessPending.value || chosenSteps.value.length === 0 || isDisabled.value) return
  running.value = true
  runningStepId.value = ''
  runError.value = ''
  // 清除上次结果：请求失败（如 400）后不得把旧结果、统计与发布入口
  // 当作本次执行结果展示（doc 18 §5）
  runResults.value = []
  jobStatus.value = ''
  const ordered = executionOrder(chosenSteps.value)
  const failedInRun = new Set<string>()
  let activeJobId = rerunJobId.value ? jobId.value : ''
  try {
    // doc 21.1 §8.4：仅当勾选 domain 且本次会话保存过描述时成对携带；
    // 未保存则不携带，由后端按 db_desc → 数据源描述解析。
    const carryDomainFields =
      chosenSteps.value.includes('domain') && savedDescription.value !== null
    for (const step of ordered) {
      const blocked = upstreamBlockedMessage(step, ordered, failedInRun)
      if (blocked) {
        runResults.value = [
          ...runResults.value,
          {
            step,
            status: 'failed',
            message: blocked,
            finished_at: new Date().toISOString(),
          },
        ]
        failedInRun.add(step)
        continue
      }
      runningStepId.value = step
      const response = await createCollectionJob(
        sourceId.value,
        [step],
        activeJobId,
        carryDomainFields ? (savedDescription.value ?? undefined) : undefined,
        carryDomainFields ? savedSync.value : false,
      )
      activeJobId = response.job_id
      jobId.value = response.job_id
      runResults.value = [...runResults.value, ...response.results]
      for (const row of response.results) {
        if (row.status === 'failed') failedInRun.add(row.step)
      }
    }
    if (activeJobId) {
      await refreshJobSummary(activeJobId)
      if (rerunJobId.value) await refreshSqlReadiness()
    }
  } catch (error) {
    runError.value = error instanceof ApiError ? error.message : '采集作业执行失败'
  } finally {
    running.value = false
    runningStepId.value = ''
  }
}

function leaveMissing() {
  return router.replace({ path: '/data-sources', query: { notice: 'not_found' } })
}

function goBack() {
  const previous = window.history.state?.back
  if (typeof previous === 'string' && previous.length > 0) {
    router.back()
    return
  }
  router.push(`/data-sources/${sourceId.value}/collection-jobs`)
}

async function load() {
  loading.value = true
  loadError.value = ''
  source.value = null
  selected.value = {}
  running.value = false
  jobId.value = ''
  runError.value = ''
  runResults.value = []
  jobStatus.value = ''
  rerunSteps.value = new Set()
  sqlReadiness.value = null
  sqlReadinessError.value = ''
  sqlReadinessPending.value = false
  ++readinessRequest
  descriptionBaseline.value = ''
  savedDescription.value = null
  savedSync.value = false
  descriptionDialogOpen.value = false
  try {
    const loaded = await getDataSource(sourceId.value)
    source.value = loaded
    if (rerunJobId.value) {
      const jobs = await listCollectionJobs(sourceId.value)
      const job = jobs.items.find((item) => item.job_id === rerunJobId.value)
      if (!job) {
        loadError.value = '指定的采集作业不存在'
        return
      }
      jobId.value = job.job_id
      rerunSteps.value = new Set(job.steps)
      // doc 21.1 §8.3：基准值 = 重跑 job.domain_description ?? 源描述
      descriptionBaseline.value = job.domain_description ?? loaded.description ?? ''
      selected.value = storedSelection(job.steps)
      await refreshSqlReadiness()
      if (job.steps.includes('sql') && sqlAvailable.value) {
        selected.value = { ...selected.value, sql: true }
      }
    } else {
      descriptionBaseline.value = loaded.description ?? ''
    }
  } catch (error) {
    if (
      error instanceof ApiError &&
      (error.code === 'not_found' || error.code === 'invalid_request')
    ) {
      await leaveMissing()
      return
    }
    loadError.value = error instanceof ApiError ? error.message : '数据源信息加载失败'
  } finally {
    loading.value = false
  }
}

watch([sourceId, rerunJobId], load, { immediate: true })
</script>

<template>
  <div class="ds-new-job">
    <header class="ds-new-job-header">
      <div>
        <span class="ds-kicker">NEW COLLECTION JOB</span>
        <h1>从数据源新建数据采集作业</h1>
        <p>
          数据源已选定：{{ source?.display_name || '…' }}。勾选需要执行的采集与画像步骤后执行。
        </p>
      </div>
      <div class="ds-new-job-actions">
        <button type="button" class="ds-new-job-back" @click="goBack">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
          返回
        </button>
        <button
          type="button"
          class="ds-new-job-run"
          :disabled="loading || !source || running || sqlReadinessPending || isDisabled || chosenSteps.length === 0"
          @click="runJob"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z" /></svg>
          {{ running ? '执行中' : '执行' }}
        </button>
      </div>
    </header>

    <p v-if="loading" class="ds-jobs-state">正在读取数据源…</p>
    <p v-else-if="loadError" class="ds-jobs-state error">{{ loadError }}</p>

    <template v-else-if="source">
      <p v-if="isDisabled" class="ds-banner" role="alert">
        该数据源已禁用，不能创建新的采集作业或画像
      </p>
      <section class="ds-new-job-bar">
        <span class="ds-new-job-bar-title">作业信息</span>
        <div class="ds-new-job-meta">
          <span class="ds-new-job-ico" aria-hidden="true">
            <svg viewBox="0 0 24 24"><ellipse cx="12" cy="6" rx="8" ry="3" /><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6" /><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></svg>
          </span>
          <p>
            <span>数据源 SOURCE ID</span>
            <strong>{{ source.source_id }}</strong>
          </p>
        </div>
        <span class="ds-new-job-split"></span>
        <div class="ds-new-job-meta">
          <span class="ds-new-job-ico" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M4 9h4" /><path d="M4 15h4" /><path d="M8 5 16 12 8 19" /><path d="M16 9h4" /><path d="M16 15h4" /></svg>
          </span>
          <p>
            <span>作业 JOB ID</span>
            <strong :class="{ pending: !jobId }">{{ jobId || '执行时自动生成' }}</strong>
          </p>
        </div>
        <span class="ds-new-job-split"></span>
        <div class="ds-new-job-picked">
          <span>已勾选任务</span>
          <span v-if="selectedChips.length === 0" class="ds-new-job-none">未勾选</span>
          <span v-for="chip in selectedChips" :key="chip" class="ds-new-job-chip">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
            {{ chip }}
          </span>
        </div>
      </section>

      <section class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">DATA SOURCE INFO</span>
            <h2>数据源基本信息</h2>
          </div>
          <RouterLink class="ds-new-job-edit" :to="`/data-sources/${source.source_id}/edit`">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" /></svg>
            修改数据源
          </RouterLink>
        </div>
        <div class="ds-jobs-facts">
          <p><span>SOURCE ID</span><strong>{{ source.source_id }}</strong></p>
          <p><span>数据源名称</span><strong>{{ source.display_name }}</strong></p>
          <p><span>HOST : PORT</span><strong>{{ source.host }}:{{ source.port }}</strong></p>
          <p><span>数据库名称</span><strong>{{ source.db_name }}</strong></p>
          <p><span>数据库类型 / 版本</span><strong>{{ dbTypeVersion }}</strong></p>
          <p><span>描述</span><strong>{{ source.description || '—' }}</strong></p>
        </div>
      </section>

      <section class="ds-jobs-card ds-new-job-steps">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">COLLECTION STEPS</span>
            <h2>数据采集与画像的步骤</h2>
            <p>采集、抽样画像、关联分析与 RAG 元数据生成。</p>
          </div>
          <div class="ds-new-job-step-actions">
            <button type="button" @click="selectAvailable(true)">全选</button>
            <button type="button" @click="selectAvailable(false)">清空</button>
          </div>
        </div>
        <p v-if="jsonWithoutDdlHint" class="ds-new-job-hint" role="status">
          {{ jsonWithoutDdlHint }}
        </p>
        <p v-if="mdWithoutJsonHint" class="ds-new-job-hint" role="status">
          {{ mdWithoutJsonHint }}
        </p>
        <p v-if="domainHint" class="ds-new-job-hint" role="status">
          {{ domainHint }}
        </p>
        <p v-if="domainReadsExistingMdHint" class="ds-new-job-hint" role="status">
          {{ domainReadsExistingMdHint }}
        </p>
        <p v-if="dimConfigHint" class="ds-new-job-hint" role="status">
          {{ dimConfigHint }}
        </p>
        <p v-if="dimConfigReadsExistingJsonHint" class="ds-new-job-hint" role="status">
          {{ dimConfigReadsExistingJsonHint }}
        </p>
        <p v-if="relHint" class="ds-new-job-hint" role="status">
          {{ relHint }}
        </p>
        <p v-if="relReadsExistingHint" class="ds-new-job-hint" role="status">
          {{ relReadsExistingHint }}
        </p>
        <p v-if="sqlHint" class="ds-new-job-hint" role="status">
          {{ sqlHint }}
        </p>
        <div class="ds-new-job-grid">
          <article v-for="group in STEP_GROUPS" :key="group.id" class="ds-new-job-group">
            <header>
              <button
                type="button"
                class="nj-check lg"
                :class="{ on: groupOn(group) }"
                :disabled="enabledSteps(group).length === 0"
                :aria-pressed="groupOn(group)"
                @click="toggleGroup(group)"
              >
                <svg v-if="groupOn(group)" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
              </button>
              <strong>{{ group.title }}</strong>
              <em>{{ group.steps.length }} 项</em>
            </header>
            <ul>
              <li v-for="step in group.steps" :key="step.id" :class="{ off: !isStepEnabled(step) }">
                <button
                  type="button"
                  class="nj-check"
                  :class="{ on: Boolean(selected[step.id]) }"
                  :disabled="!isStepEnabled(step)"
                  :aria-pressed="Boolean(selected[step.id])"
                  @click="toggleStep(step)"
                >
                  <svg v-if="selected[step.id]" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
                </button>
                <template v-if="step.id === 'domain'">
                  <span class="ds-new-job-domain-label">--generate-domains</span>
                  <button type="button" class="ds-new-job-desc-link" @click="openDescriptionDialog">
                    {{ descriptionLinkText }}
                  </button>
                </template>
                <span v-else>{{ step.label }}</span>
              </li>
            </ul>
          </article>
        </div>
      </section>

      <section class="ds-jobs-card ds-new-job-steps">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">EXECUTION RESULT</span>
            <h2>执行结果</h2>
            <p>步骤按顺序逐个执行；每完成一步即显示该步结果，全部完成后显示汇总。</p>
          </div>
          <span
            v-if="runResults.length"
            class="ds-new-job-result-pill"
            :class="failedCount ? 'bad' : 'ok'"
          >
            {{ successCount }} 成功 · {{ failedCount }} 失败
          </span>
        </div>
        <p v-if="runError" class="ds-jobs-state error">{{ runError }}</p>
        <p v-if="running && !runResults.length" class="ds-new-job-empty">
          正在执行所选采集步骤…
        </p>
        <p v-else-if="running && runningStepLabel" class="ds-new-job-hint" role="status">
          正在执行：{{ runningStepLabel }}…
        </p>
        <div v-if="resultGroups.length" class="ds-new-job-grid">
          <article v-for="group in resultGroups" :key="group.title" class="ds-new-job-group">
            <header><strong>{{ group.title }}</strong></header>
            <ul class="result-list">
              <li v-for="row in group.rows" :key="row.step" :class="row.status">
                <span>{{ row.label }}</span>
                <em>{{ row.status === 'success' ? '执行成功' : '执行失败' }}</em>
                <time>{{ displayDate(row.finished_at) }}</time>
                <small v-if="row.message">{{ row.message }}</small>
              </li>
            </ul>
          </article>
        </div>
        <p v-if="!running && !runError && !resultGroups.length" class="ds-new-job-empty">
          尚未执行
        </p>
      </section>

      <p
        v-if="runResults.length && jobId && (jobStatus === 'failed' || jobStatus === 'running')"
        class="ds-new-job-hint"
        role="status"
      >
        该采集作业{{ jobStatus === 'running' ? '仍有步骤正在运行' : '存在失败的步骤' }}，暂不能发布；
        待相关步骤成功后再进入发布。
      </p>
      <div v-if="canPublishNext" class="ds-new-job-next">
        <RouterLink :to="`/data-sources/${sourceId}/collection-jobs/${jobId}/publish`">
          下一步：执行画像发布
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg>
        </RouterLink>
      </div>
    </template>

    <DomainDescriptionDialog
      v-if="descriptionDialogOpen"
      :initial-text="savedDescription ?? descriptionBaseline"
      :initial-sync="savedSync"
      @save="onDescriptionSaved"
      @cancel="descriptionDialogOpen = false"
    />
  </div>
</template>
