<script setup lang="ts">
/**
 * 工作区详情·概览（doc 6.3 §4 编号 23）：基本信息、编辑入口、
 * 采集/发布作业概览卡片（作业数、累计执行 total、最近一次运行）、
 * 最近运行。查看详情进入已有的作业列表页。
 * §4.5：不展示创建人。执行状态分布只在取齐全部运行后计数，不用当前页凑数。
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { displayDate, displayDateMs } from '../api/format'
import { ApiError } from '../api/http'
import type { JobItem, RunListItem, RunStatus, WorkspaceItem } from '../api/types'
import { listJobs, listWorkspaceRuns, patchWorkspace, runIdOf } from '../api/workspaces'
import DeleteResourceDialog from '../components/DeleteResourceDialog.vue'
import StepDetailDrawer from '../components/StepDetailDrawer.vue'
import { useRunPolling } from '../composables/useRunPolling'
import { useWorkspaceContext } from '../composables/useWorkspaceContext'
import RunDrawer from '../components/RunDrawer.vue'
import PageBreadcrumbs from '../components/PageBreadcrumbs.vue'
import { runStatusText, triggerTypeText } from '../utils/steps'
import { hierarchyFrom, passThroughFrom, resolveBack, withFrom, workspacesCrumbTo } from '../navigation/from'

const route = useRoute()
const router = useRouter()
const deleteOpen = ref(false)
const resumeTaskId = ref<string | undefined>(undefined)

function onWorkspaceDeleted() {
  deleteOpen.value = false
  resumeTaskId.value = undefined
  // doc 9 §7.6：删除工作区成功 → 工作区详情的返回目标（from 指向 /workspaces… 时回那个列表）
  void router.push(backSpec.value.target ?? '/workspaces')
}

// doc 8.1 §7.0/§7.3：按任务 action 分流——工作区删除本地弹窗恢复；
// 源删除/知识清理是父级范围任务，跳转源详情对应入口处理
function openTaskProgress(task: NonNullable<WorkspaceItem['deletion_task']>) {
  if (task.action === 'delete_workspace') {
    resumeTaskId.value = task.task_id
    deleteOpen.value = true
    return
  }
  void router.push(`/data-sources/${task.source_id}`)
}

function taskEntryLabel(
  task: NonNullable<WorkspaceItem['deletion_task']>,
): string {
  if (task.action === 'delete_workspace') return '查看工作区删除进度'
  if (task.action === 'delete_source') return '前往数据源处理删除任务'
  return '前往数据源处理知识清理任务'
}
const workspaceId = computed(() =>
  typeof route.params.workspaceId === 'string' ? route.params.workspaceId : '',
)
const { workspace, source, loading, error, sourceDisabled, reload } = useWorkspaceContext(
  () => workspaceId.value,
)

const NAME_MAX = 200
const DESC_MAX = 1000

// ---- 编辑工作区基本信息（PATCH 仅名称/说明，不改绑来源） ----
const editOpen = ref(false)
const editName = ref('')
const editDesc = ref('')
const editSaving = ref(false)
const editError = ref('')

const nameCount = computed(() => [...editName.value].length)
const descCount = computed(() => [...editDesc.value].length)

function openEdit() {
  if (!workspace.value) return
  editName.value = workspace.value.workspace_name
  editDesc.value = workspace.value.workspace_desc ?? ''
  editError.value = ''
  editOpen.value = true
}

async function saveEdit() {
  if (editSaving.value || !workspace.value) return
  const name = editName.value.trim()
  if (!name) {
    editError.value = '工作区名称不能为空'
    return
  }
  if (nameCount.value > NAME_MAX) {
    editError.value = `工作区名称不能超过 ${NAME_MAX} 个字符`
    return
  }
  if (descCount.value > DESC_MAX) {
    editError.value = `工作区说明不能超过 ${DESC_MAX} 个字符`
    return
  }
  editSaving.value = true
  editError.value = ''
  try {
    await patchWorkspace(workspace.value.workspace_id, {
      workspace_name: name,
      workspace_desc: editDesc.value.trim() || null,
    })
    editOpen.value = false
    await reload()
  } catch (cause) {
    editError.value = cause instanceof ApiError ? cause.message : '工作区信息保存失败。'
  } finally {
    editSaving.value = false
  }
}

// ---- 作业数与累计执行（§4.5：作业数=作业列表计数；累计=运行列表 total） ----
const RECENT_LIMIT = 5
const DIST_PAGE = 100

const STATUS_ORDER: RunStatus[] = ['pending', 'running', 'success', 'partial_success', 'failed']
const STATUS_COLOR: Record<RunStatus, string> = {
  pending: '#66788F',
  running: '#D97706',
  success: '#0E9F8A',
  partial_success: '#2E7CD6',
  failed: '#DC4A4A',
}

const collectionJobs = ref<JobItem[]>([])
const publishJobs = ref<JobItem[]>([])
const collectionRuns = ref<RunListItem[]>([])
const publishRuns = ref<RunListItem[]>([])
const collectionTotal = ref<number | null>(null)
const publishTotal = ref<number | null>(null)
const collectionCounts = ref<Record<RunStatus, number> | null>(null)
const publishCounts = ref<Record<RunStatus, number> | null>(null)
const runsError = ref('')

function emptyCounts(): Record<RunStatus, number> {
  return { pending: 0, running: 0, success: 0, partial_success: 0, failed: 0 }
}

function countStatuses(runs: RunListItem[]): Record<RunStatus, number> {
  const counts = emptyCounts()
  for (const run of runs) {
    if (run.status in counts) counts[run.status] += 1
  }
  return counts
}

async function loadAllRuns(kind: 'collection' | 'publish') {
  const first = await listWorkspaceRuns(kind, workspaceId.value, { limit: DIST_PAGE })
  const items = [...first.items]
  let offset = items.length
  while (items.length < first.total && offset < first.total) {
    const page = await listWorkspaceRuns(kind, workspaceId.value, { limit: DIST_PAGE, offset })
    if (page.items.length === 0) break
    items.push(...page.items)
    offset += page.items.length
  }
  return {
    recent: items.slice(0, RECENT_LIMIT),
    total: first.total,
    counts: items.length === first.total ? countStatuses(items) : null,
  }
}

async function loadRuns() {
  runsError.value = ''
  try {
    const [jobsC, jobsP, runsC, runsP] = await Promise.all([
      listJobs('collection', workspaceId.value),
      listJobs('publish', workspaceId.value),
      loadAllRuns('collection'),
      loadAllRuns('publish'),
    ])
    collectionJobs.value = jobsC.items
    publishJobs.value = jobsP.items
    collectionRuns.value = runsC.recent
    publishRuns.value = runsP.recent
    collectionTotal.value = runsC.total
    publishTotal.value = runsP.total
    collectionCounts.value = runsC.counts
    publishCounts.value = runsP.counts
  } catch (cause) {
    runsError.value = cause instanceof ApiError ? cause.message : '作业与运行统计加载失败。'
  }
}

watch(workspaceId, () => {
  if (workspaceId.value) void loadRuns()
}, { immediate: true })

// ---- 最近运行抽屉 + 步骤详情 ----
const drawer = { collection: useRunPolling('collection'), publish: useRunPolling('publish') }
const openRunKind = ref<'collection' | 'publish'>('collection')
const stepDrawerStep = ref('')

function latestJobUpdate(jobs: JobItem[]): string {
  return jobs.reduce((latest, job) => (job.updated_at > latest ? job.updated_at : latest), '')
}

function dateLines(value: string | null | undefined): { date: string; time: string } | null {
  const text = displayDate(value || '')
  if (text === '—') return null
  const [date, time = ''] = text.split(' ')
  return { date, time }
}

const jobCards = computed(() => {
  const id = workspace.value?.workspace_id || workspaceId.value
  return [
    {
      kind: 'collection' as const,
      kicker: 'COLLECTION JOBS',
      title: '数据采集作业',
      to: `/workspaces/${id}/collection`,
      jobCount: collectionJobs.value.length,
      total: collectionTotal.value,
      latest: collectionRuns.value[0] ?? null,
      latestTime: dateLines(collectionRuns.value[0]?.created_at),
      updated: dateLines(latestJobUpdate(collectionJobs.value)),
      counts: collectionCounts.value,
    },
    {
      kind: 'publish' as const,
      kicker: 'PUBLISH JOBS',
      title: '数据发布作业',
      to: `/workspaces/${id}/publish`,
      jobCount: publishJobs.value.length,
      total: publishTotal.value,
      latest: publishRuns.value[0] ?? null,
      latestTime: dateLines(publishRuns.value[0]?.created_at),
      updated: dateLines(latestJobUpdate(publishJobs.value)),
      counts: publishCounts.value,
    },
  ]
})

function openRun(kind: 'collection' | 'publish', runId: string) {
  openRunKind.value = kind
  stepDrawerStep.value = ''
  drawer[kind].open(runId)
}

// ---- doc 9 §7：返回与来源 ----

function queryFrom(): string | null {
  const raw = route.query.from
  return typeof raw === 'string' ? raw : null
}

/** §7.4：工作区详情认 /workspaces… 来源（恢复筛选与页码），默认 /workspaces */
const backSpec = computed(() => resolveBack('workspace-home', queryFrom(), {}))

const crumbSegments = computed(() => [
  { label: '工作区管理', to: workspacesCrumbTo(queryFrom()) },
  { label: workspace.value?.workspace_name ?? '工作区' },
])
</script>

<template>
  <div class="ds-jobs">
    <PageBreadcrumbs :segments="crumbSegments" />
    <header class="ds-jobs-header">
      <div>
        <span class="ds-kicker">WORKSPACE OVERVIEW</span>
        <h1>工作区详情</h1>
        <p>查看工作区的基本信息，以及数据采集作业与数据发布作业的概览统计。</p>
      </div>
      <div class="ws-home-actions">
        <button
          v-if="workspace?.deletion_task"
          type="button"
          class="ds-danger"
          @click="openTaskProgress(workspace.deletion_task!)"
        >
          {{ taskEntryLabel(workspace.deletion_task!) }}
        </button>
        <RouterLink class="ds-jobs-back" :to="backSpec.target!">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
          {{ backSpec.label }}
        </RouterLink>
        <button
          v-if="workspace"
          type="button"
          class="ds-danger"
          @click="deleteOpen = true; resumeTaskId = undefined"
        >
          删除工作区
        </button>
      </div>
    </header>

    <p v-if="loading" class="ds-jobs-state">正在读取工作区…</p>
    <p v-else-if="error" class="ds-jobs-state error">{{ error }}</p>

    <template v-else-if="workspace && source">
      <p v-if="sourceDisabled" class="ds-banner" role="alert">
        该工作区所属数据源已禁用，不能发起新的采集或发布运行；已创建的运行记录仍可查看。
      </p>

      <section class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">WORKSPACE INFO</span>
            <h2>工作区基本信息</h2>
          </div>
          <button type="button" class="ds-new-job-edit" @click="openEdit">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
            编辑工作区
          </button>
        </div>
        <div class="ds-jobs-facts ws-info-facts">
          <p><span>工作区名称</span><strong class="ws-plain">{{ workspace.workspace_name }}</strong></p>
          <p><span>WORKSPACE ID</span><strong>{{ workspace.workspace_id }}</strong></p>
          <p><span>数据源名称</span><strong class="ws-plain">{{ source.display_name }}</strong></p>
          <p><span>数据源编码</span><strong>{{ workspace.source_id }}</strong></p>
          <p><span>更新时间</span><strong>{{ displayDate(workspace.updated_at) }}</strong></p>
        </div>
        <div class="ws-purpose">
          <span>用途说明</span>
          <p>{{ workspace.workspace_desc || '—' }}</p>
        </div>
      </section>

      <div class="ws-job-overview">
        <section
          v-for="card in jobCards"
          :key="card.kind"
          class="ds-jobs-card ws-job-card"
        >
          <div class="ds-jobs-list-head">
            <div>
              <span class="ds-kicker ds-kicker-sm">{{ card.kicker }}</span>
              <h2>{{ card.title }}</h2>
            </div>
            <div class="ws-job-card-actions">
              <RouterLink
                class="ds-jobs-create ws-create-link"
                :to="withFrom(`${card.to}/new`, hierarchyFrom(queryFrom()))"
                :aria-label="`新建${card.title}`"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14" /><path d="M5 12h14" /></svg>
                新建作业
              </RouterLink>
              <RouterLink class="ws-detail-link" :to="withFrom(card.to, hierarchyFrom(queryFrom()))">
                查看详情
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg>
              </RouterLink>
            </div>
          </div>
          <div class="ws-mini-stats">
            <p class="narrow"><span>作业数量</span><strong>{{ card.jobCount }}</strong></p>
            <p class="narrow"><span>执行次数</span><strong>{{ card.total ?? '—' }}</strong></p>
            <p>
              <span>最近一次<br>执行时间</span>
              <strong v-if="card.latestTime" class="ws-time">
                <em>{{ card.latestTime.date }}</em>
                <em>{{ card.latestTime.time }}</em>
              </strong>
              <strong v-else>—</strong>
            </p>
            <p>
              <span>最近一次<br>执行状态</span>
              <span v-if="card.latest" class="home-status" :class="card.latest.status">{{ runStatusText(card.latest.status) }}</span>
              <strong v-else>—</strong>
            </p>
            <p>
              <span>更新时间</span>
              <strong v-if="card.updated" class="ws-time">
                <em>{{ card.updated.date }}</em>
                <em>{{ card.updated.time }}</em>
              </strong>
              <strong v-else>—</strong>
            </p>
          </div>
          <div class="ws-dist-rule"></div>
          <div class="ws-dist">
            <div class="ws-dist-head">
              <span>执行状态分布</span>
              <em>共 {{ card.total ?? '—' }} 次</em>
            </div>
            <div class="ws-dist-bar" aria-hidden="true">
              <i
                v-for="status in STATUS_ORDER"
                v-show="card.counts && card.counts[status] > 0"
                :key="status"
                :style="{ flexGrow: card.counts?.[status] ?? 0, background: STATUS_COLOR[status] }"
              ></i>
            </div>
            <div class="ws-dist-legend">
              <div v-for="status in STATUS_ORDER" :key="status">
                <span><i :style="{ background: STATUS_COLOR[status] }"></i>{{ status }}</span>
                <strong>{{ card.counts ? card.counts[status] : '—' }}</strong>
              </div>
            </div>
          </div>
        </section>
      </div>

      <section class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">RECENT RUNS</span>
            <h2>最近运行</h2>
            <p>本工作区全部作业的最近采集与发布运行；点击「执行步骤」查看运行详情。</p>
          </div>
          <button type="button" class="ds-refresh" @click="loadRuns">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M21 12a9 9 0 0 0-15.5-6.3L3 8" /><path d="M3 3v5h5" />
              <path d="M3 12a9 9 0 0 0 15.5 6.3L21 16" /><path d="M21 21v-5h-5" />
            </svg>
            刷新
          </button>
        </div>
        <p v-if="runsError" class="ds-jobs-state error">{{ runsError }}</p>
        <div class="ws-recent-grid">
          <div class="ws-recent-col">
            <h3>采集运行</h3>
            <p v-if="collectionRuns.length === 0" class="ds-jobs-empty">还没有采集运行</p>
            <div v-for="run in collectionRuns" :key="runIdOf(run)" class="ws-run-row">
              <span class="ws-run-main">
                <strong>{{ run.job_name || run.job_id }}</strong>
                <span class="ws-run-id">{{ runIdOf(run) }}</span>
              </span>
              <span class="ws-run-side">
                <span class="ws-run-mode">执行模式 {{ triggerTypeText(run.trigger_type) }}</span>
                <span class="ws-run-time">执行时间 {{ displayDateMs(run.created_at ?? '') }}</span>
                <span class="home-status" :class="run.status">{{ runStatusText(run.status) }}</span>
                <button
                  type="button"
                  class="ws-run-action"
                  @click="openRun('collection', runIdOf(run))"
                >执行步骤</button>
                <RouterLink
                  class="ws-run-action"
                  :to="withFrom(`/workspaces/${workspaceId}/collection/jobs/${run.job_id}`, passThroughFrom(queryFrom()))"
                >作业详情</RouterLink>
              </span>
            </div>
          </div>
          <div class="ws-recent-col">
            <h3>发布运行</h3>
            <p v-if="publishRuns.length === 0" class="ds-jobs-empty">还没有发布运行</p>
            <div v-for="run in publishRuns" :key="runIdOf(run)" class="ws-run-row">
              <span class="ws-run-main">
                <strong>{{ run.job_name || run.job_id }}</strong>
                <span class="ws-run-id">{{ runIdOf(run) }}</span>
              </span>
              <span class="ws-run-side">
                <span class="ws-run-mode">执行模式 {{ triggerTypeText(run.trigger_type) }}</span>
                <span class="ws-run-time">执行时间 {{ displayDateMs(run.created_at ?? '') }}</span>
                <span class="home-status" :class="run.status">{{ runStatusText(run.status) }}</span>
                <button
                  type="button"
                  class="ws-run-action"
                  @click="openRun('publish', runIdOf(run))"
                >执行步骤</button>
                <RouterLink
                  class="ws-run-action"
                  :to="withFrom(`/workspaces/${workspaceId}/publish/jobs/${run.job_id}`, passThroughFrom(queryFrom()))"
                >作业详情</RouterLink>
              </span>
            </div>
          </div>
        </div>
      </section>
    </template>

    <!-- 编辑工作区信息弹窗 -->
    <div v-if="editOpen" class="dialog-backdrop" @click.self="editOpen = false">
      <div class="dialog-panel with-close" role="dialog" aria-modal="true" aria-label="编辑工作区信息">
        <button type="button" class="dialog-close" aria-label="关闭" @click="editOpen = false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></button>
        <p>编辑工作区信息</p>
        <p class="dialog-note">仅修改名称与说明；工作区不可改绑数据源。</p>
        <label class="field">
          <span>工作区名称</span>
          <input v-model="editName" type="text" :disabled="editSaving" />
          <span class="character-count">{{ nameCount }}/{{ NAME_MAX }}</span>
        </label>
        <label class="field">
          <span>工作区说明（可选）</span>
          <textarea v-model="editDesc" rows="3" :disabled="editSaving"></textarea>
          <span class="character-count">{{ descCount }}/{{ DESC_MAX }}</span>
        </label>
        <p v-if="editError" class="status-message" role="alert">{{ editError }}</p>
        <div class="dialog-actions">
          <button type="button" class="button button-outline" :disabled="editSaving" @click="editOpen = false">取消</button>
          <button type="button" class="button button-primary" :disabled="editSaving" @click="saveEdit">保存</button>
        </div>
      </div>
    </div>

    <RunDrawer
      v-if="drawer[openRunKind].runId.value"
      :kind="openRunKind"
      :run="drawer[openRunKind].detail.value"
      :loading="drawer[openRunKind].loading.value"
      :error="drawer[openRunKind].error.value"
      @close="drawer[openRunKind].close(); stepDrawerStep = ''"
      @retry="drawer[openRunKind].retry()"
      @open-step="stepDrawerStep = $event"
    />

    <StepDetailDrawer
      v-if="stepDrawerStep && drawer[openRunKind].runId.value"
      :kind="openRunKind"
      :run="drawer[openRunKind].detail.value"
      :step-name="stepDrawerStep"
      :loading="drawer[openRunKind].loading.value"
      :error="drawer[openRunKind].error.value"
      @close="stepDrawerStep = ''"
      @select-step="stepDrawerStep = $event"
    />
  </div>
  <DeleteResourceDialog
    v-if="deleteOpen && workspace"
    kind="workspace"
    :target-id="workspaceId"
    :display-name="workspace.workspace_name"
    :resume-task-id="resumeTaskId"
    @cancel="deleteOpen = false; resumeTaskId = undefined"
    @done="onWorkspaceDeleted"
  />
</template>

<style scoped>
/* 页头右侧按钮组与标题块顶对齐，沿用已确认的返回按钮布局。 */
.ds-jobs .ds-jobs-header {
  align-items: flex-start;
}
.ws-home-actions {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.ws-info-facts {
  grid-template-columns: repeat(5, minmax(0, 1fr));
}
.ws-plain {
  font-family: "Noto Sans SC", system-ui, sans-serif;
}
.ws-purpose {
  background: #eef3f9;
  border-radius: 10px;
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ws-purpose span {
  color: #66788f;
  font-size: 10px;
  letter-spacing: 1px;
}
.ws-purpose p {
  margin: 0;
  color: #17293f;
  font-size: 12px;
  line-height: 1.6;
}
.ws-job-overview {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
}
.ws-detail-link {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: #2e7cd6;
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}
.ws-detail-link svg {
  width: 12px;
  height: 12px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.ws-job-card .ds-jobs-list-head {
  align-items: center;
}
.ws-job-card-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.ws-create-link {
  padding: 3px 8px;
  white-space: nowrap;
}
.ws-mini-stats {
  display: flex;
  gap: 10px;
  align-items: stretch;
}
.ws-mini-stats p {
  margin: 0;
  flex: 1 1 0;
  background: #eef3f9;
  border-radius: 10px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.ws-mini-stats p.narrow {
  flex: 0 0 64px;
}
.ws-mini-stats p > span:first-child {
  color: #66788f;
  font-size: 10px;
  line-height: 14px;
}
.ws-time {
  display: flex;
  flex-direction: column;
  gap: 1px;
  font-style: normal;
}
.ws-time em {
  font-style: normal;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  font-weight: 700;
  line-height: 15px;
}
.ws-dist-rule {
  height: 1px;
  background: #d9e3ef;
}
.ws-dist {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ws-dist-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.ws-dist-head span {
  color: #66788f;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1px;
}
.ws-dist-head em {
  font-style: normal;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 10px;
  color: #66788f;
}
.ws-dist-bar {
  display: flex;
  height: 10px;
  border-radius: 5px;
  overflow: hidden;
  background: #eef3f9;
}
.ws-dist-bar i {
  display: block;
  height: 10px;
  min-width: 4px;
}
.ws-dist-legend {
  display: flex;
  gap: 8px;
}
.ws-dist-legend > div {
  flex: 1 1 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.ws-dist-legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: #66788f;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 9px;
  white-space: nowrap;
}
.ws-dist-legend i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: 0 0 7px;
}
.ws-dist-legend strong {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 13px;
  font-weight: 700;
  color: #17293f;
}
.ws-recent-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
}
.ws-recent-col {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.ws-recent-col h3 {
  margin: 0;
  font-size: 14px;
}
.ws-run-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  width: 100%;
  text-align: left;
  padding: 10px 14px;
  border: 1px solid #e1e9f4;
  border-radius: 12px;
  background: #fcfdff;
}
.ws-run-main {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.ws-run-main strong {
  font-size: 13px;
}
.ws-run-id {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 10px;
  color: #66788f;
  overflow-wrap: anywhere;
}
.ws-run-side {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}
.ws-run-mode {
  color: #66788f;
  font-size: 11px;
  white-space: nowrap;
}
.ws-run-action {
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
  border: 1px solid #c5d7ee;
  background: #fff;
  color: #2e7cd6;
  border-radius: 11px;
  padding: 4px 10px;
  font-size: 11px;
  font-weight: 700;
  line-height: 1.2;
  text-decoration: none;
  white-space: nowrap;
  cursor: pointer;
}
.ws-run-action:hover {
  background: #e3eefb;
  border-color: #2e7cd6;
}
.ws-run-action:focus-visible {
  outline: 2px solid #2e7cd6;
  outline-offset: 2px;
}
.ws-run-time {
  color: #66788f;
  font-size: 11px;
  white-space: nowrap;
}
.ws-mini-stats strong {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 12px;
  font-weight: 700;
  overflow-wrap: anywhere;
}
@media (max-width: 1100px) {
  .ws-info-facts {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .ws-mini-stats {
    flex-wrap: wrap;
  }
  .ws-mini-stats p,
  .ws-mini-stats p.narrow {
    flex: 1 1 120px;
  }
}
@media (max-width: 800px) {
  .ws-job-overview,
  .ws-recent-grid,
  .ws-info-facts {
    grid-template-columns: 1fr;
  }
}
</style>
