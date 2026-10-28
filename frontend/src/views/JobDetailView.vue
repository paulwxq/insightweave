<script setup lang="ts">
/**
 * 作业详情（doc 6.3 §4 编号 26.1/27.1，设计稿 34）：作业信息、不可变版本
 * （查看参数/保存新版本）、运行历史与运行详情抽屉；发布侧执行入口在
 * 配置版本卡右下（发布校验 + 执行），发布预检卡按需展开。
 * 步骤详情抽屉使用 ?run_id=<uuid>&step=<step> 深链（doc 6.3 §4）。
 * 发布预检按当前版本步骤过滤，单行左对齐（2026-10-26 定案：预检行不提供
 * 编辑入口，Domain/Rel/Dim 编辑只在「创建新版本」编辑器步骤行，doc 6.2 §9.1.6）。
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { displayDate, displayDateLoose } from '../api/format'
import { ApiError } from '../api/http'
import type {
  JobKind,
  PublishCheckResponse,
  PublishCheckDirectory,
  PublishCheckStep,
} from '../api/types'
import { addRevision, getPublishCheck, patchJob } from '../api/workspaces'
import JobConfigEditor from '../components/JobConfigEditor.vue'
import RevisionParamsDialog from '../components/RevisionParamsDialog.vue'
import RunDrawer from '../components/RunDrawer.vue'
import StepDetailDrawer from '../components/StepDetailDrawer.vue'
import EmptyState from '../components/EmptyState.vue'
import { useJobDetail } from '../composables/useJobDetail'
import { useRunPolling } from '../composables/useRunPolling'
import { useWorkspaceContext } from '../composables/useWorkspaceContext'
import {
  PUBLISH_STEPS,
  PUBLISH_STEP_INPUT_DIRS,
  runStatusText,
} from '../utils/steps'
import { runIdOf } from '../api/workspaces'

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
const basePath = computed(
  () => `/workspaces/${workspaceId.value}/${props.kind === 'collection' ? 'collection' : 'publish'}`,
)

const ctx = useWorkspaceContext(() => workspaceId.value)
// 运行历史页码写入 query（doc 6.3 §4.1：分页可刷新恢复；page=1 时省略）
const runsPageNo = computed(() => {
  const raw = Number(Array.isArray(route.query.page) ? route.query.page[0] : route.query.page)
  return Number.isInteger(raw) && raw >= 1 ? raw : 1
})
const detail = useJobDetail(
  props.kind,
  () => jobId.value,
  () => basePath.value,
  () => runsPageNo.value,
)

function gotoRunsPage(page: number) {
  const next = { ...route.query }
  if (page > 1) next.page = String(page)
  else delete next.page
  void router.replace({ query: next })
}

const revisionSteps = computed(() => detail.selectedRevision.value?.steps ?? [])

// 版本 step_config.domain.description（仅采集 domain 首次生成输入存在）
const revisionDomainDescription = computed(() => {
  const domain = detail.selectedRevision.value?.step_config?.domain as
    | Record<string, unknown>
    | undefined
  return typeof domain?.description === 'string' ? domain.description : ''
})
const revisionHasDomainDescription = computed(() => revisionDomainDescription.value !== '')

// ---- 作业信息编辑（PATCH 仅改主表信息，不新增版本） ----
const infoOpen = ref(false)
const infoName = ref('')
const infoDesc = ref('')
const infoSaving = ref(false)
const infoError = ref('')

function openInfoEdit() {
  if (!detail.job.value) return
  infoName.value = detail.job.value.job_name
  infoDesc.value = detail.job.value.job_desc ?? ''
  infoError.value = ''
  infoOpen.value = true
}

async function saveInfo() {
  if (infoSaving.value || !detail.job.value) return
  const name = infoName.value.trim()
  if (!name) {
    infoError.value = '作业名称不能为空'
    return
  }
  infoSaving.value = true
  infoError.value = ''
  try {
    await patchJob(props.kind, detail.job.value.job_id, {
      job_name: name,
      job_desc: infoDesc.value.trim() || null,
    })
    infoOpen.value = false
    await detail.loadJob()
  } catch (cause) {
    infoError.value = cause instanceof ApiError ? cause.message : '作业信息保存失败。'
  } finally {
    infoSaving.value = false
  }
}

// ---- 版本：保存新版本 ----
const editorOpen = ref(false)
const editorSaving = ref(false)
const editorError = ref('')

function openEditor() {
  editorError.value = ''
  editorOpen.value = true
}

async function saveRevision(payload: {
  steps: string[]
  step_config: Record<string, Record<string, unknown>> | null
  change_note: string | null
}) {
  if (editorSaving.value) return
  editorSaving.value = true
  editorError.value = ''
  try {
    if (props.kind === 'publish') {
      // 保存前复查（与新建发布页一致）：勾选后的产物/配置状态可能已变化，
      // 只拦所选步骤；未勾选的失败步骤不阻塞保存
      const ok = await loadCheck()
      if (!ok) {
        editorError.value = checkError.value || '发布校验失败，无法保存新版本。'
        return
      }
      const byStep = new Map<string, PublishCheckStep>(
        (check.value?.steps ?? []).map((step) => [step.step_name, step] as const),
      )
      const failing = payload.steps.filter((step) => byStep.get(step)?.ready !== true)
      if (failing.length > 0) {
        const detail = failing
          .map((step) => `${step}（${byStep.get(step)?.reason ?? '原因未知'}）`)
          .join('；')
        editorError.value = `所选步骤发布校验未通过：${detail}。请重新校验并调整勾选。`
        return
      }
    }
    const revision = await addRevision(props.kind, jobId.value, {
      job_name: detail.job.value?.job_name ?? '',
      job_desc: detail.job.value?.job_desc ?? null,
      config: payload,
    })
    editorOpen.value = false
    await detail.loadJob()
    detail.selectRevision(revision.revision_no)
  } catch (cause) {
    editorError.value = cause instanceof ApiError ? cause.message : '新版本保存失败。'
  } finally {
    editorSaving.value = false
  }
}

// ---- 执行入口：跳转 29/31 执行页（携带当前展示版本与模式） ----

function goRun(mode: 'default' | 'custom') {
  const revisionNo = detail.selectedRevisionNo.value
  if (revisionNo === null) return
  void router.push({
    path: `${basePath.value}/jobs/${jobId.value}/run`,
    query: { revision_no: String(revisionNo), mode },
  })
}

// ---- 发布预检与配置编辑（doc 6.2 §8.2.2；设计稿 34：预检卡按需展开） ----
const check = ref<PublishCheckResponse | null>(null)
const checkLoading = ref(false)
const checkError = ref('')
// 预检卡默认收起，由配置版本卡右下「发布校验」展开（非强制，不拦截执行）
const checkCardOpen = ref(false)
// 版本行「查看参数」弹窗（发布专属；三 tab 读工作区配置文件）
const paramsOpen = ref(false)

const checkByStep = computed(() => {
  const map = new Map<string, PublishCheckStep>()
  for (const step of check.value?.steps ?? []) map.set(step.step_name, step)
  return map
})
const dirByName = computed(() => {
  const map = new Map<string, PublishCheckDirectory>()
  for (const dir of check.value?.input_directories ?? []) map.set(dir.directory, dir)
  return map
})

async function loadCheck(): Promise<boolean> {
  checkLoading.value = true
  checkError.value = ''
  try {
    check.value = await getPublishCheck(workspaceId.value)
    return true
  } catch (cause) {
    checkError.value = cause instanceof ApiError ? cause.message : '发布预检读取失败。'
    return false
  } finally {
    checkLoading.value = false
  }
}

function openCheckCard() {
  checkCardOpen.value = true
  void loadCheck()
}

function dirCollectedText(dir: string): string {
  const row = dirByName.value.get(dir)
  if (!row) return '状态未知'
  if (row.collected_at) return `采集于 ${displayDateLoose(row.collected_at)}`
  if (row.marker_status === 'failed') return '最近一次采集未成功'
  if (row.error) return row.error
  return '未采集'
}

// ---- 运行历史与步骤详情抽屉（?run_id=&step= 深链） ----
const historyRun = useRunPolling(props.kind)
const stepDrawerStep = ref('')

const queryRunId = computed(() =>
  typeof route.query.run_id === 'string' ? route.query.run_id : '',
)
const queryStep = computed(() =>
  typeof route.query.step === 'string' ? route.query.step : '',
)

watch(
  queryRunId,
  (runId) => {
    if (runId && runId !== historyRun.runId.value) historyRun.open(runId)
    if (!runId && historyRun.runId.value) historyRun.close()
  },
  { immediate: true },
)
watch(
  queryStep,
  (step) => {
    stepDrawerStep.value = step
  },
  { immediate: true },
)

function openHistoryRun(runId: string) {
  void router.replace({ query: { ...route.query, run_id: runId, step: undefined } })
}

function closeHistoryRun() {
  historyRun.close()
  stepDrawerStep.value = ''
  const next = { ...route.query }
  delete next.run_id
  delete next.step
  void router.replace({ query: next })
}

function openStep(step: string) {
  void router.replace({ query: { ...route.query, step } })
}

function closeStepDrawer() {
  stepDrawerStep.value = ''
  const next = { ...route.query }
  delete next.step
  void router.replace({ query: next })
}

function switchStep(step: string) {
  void router.replace({ query: { ...route.query, step } })
}
</script>

<template>
  <div class="ds-jobs">
    <header class="ds-jobs-header">
      <div>
        <span class="ds-kicker">{{ kind === 'collection' ? 'COLLECTION JOB' : 'PUBLISH JOB' }}</span>
        <h1>{{ detail.job.value?.job_name || `${kindText}作业` }}</h1>
        <p>
          工作区 {{ ctx.workspace.value?.workspace_name || '…' }}（{{ workspaceId }}）内的
          {{ kindText }}作业。一次执行生成一个新的运行，运行结果独立保存。
        </p>
      </div>
      <div class="ws-home-actions">
        <RouterLink class="ds-jobs-back" :to="basePath">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
          返回
        </RouterLink>
      </div>
    </header>

    <p v-if="detail.loading.value" class="ds-jobs-state">正在读取作业…</p>
    <p v-else-if="detail.error.value" class="ds-jobs-state error">{{ detail.error.value }}</p>

    <template v-else-if="detail.job.value">
      <p v-if="ctx.sourceDisabled.value" class="ds-banner" role="alert">
        该工作区所属数据源已禁用，不能发起新的{{ kindText }}运行；作业与历史记录仍可查看。
      </p>

      <!-- 作业信息 -->
      <section class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">JOB INFO</span>
            <h2>作业信息</h2>
          </div>
          <button type="button" class="ds-new-job-edit" @click="openInfoEdit">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
            {{ kind === 'collection' ? '修改作业信息' : '编辑名称/说明' }}
          </button>
        </div>
        <div class="ds-jobs-facts">
          <p><span>JOB ID</span><strong>{{ detail.job.value.job_id }}</strong></p>
          <p><span>作业名称</span><strong>{{ detail.job.value.job_name }}</strong></p>
          <p><span>WORKSPACE ID</span><strong>{{ detail.job.value.workspace_id }}</strong></p>
          <p><span>最新版本</span><strong>{{ detail.job.value.latest_revision_no ?? '—' }}</strong></p>
          <p><span>创建时间</span><strong>{{ displayDate(detail.job.value.created_at) }}</strong></p>
          <p><span>更新时间</span><strong>{{ displayDate(detail.job.value.updated_at) }}</strong></p>
        </div>
        <p v-if="detail.job.value.job_desc" class="ws-home-desc">{{ detail.job.value.job_desc }}</p>
      </section>

      <!-- 版本 -->
      <section class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">REVISIONS</span>
            <h2>配置版本</h2>
            <p>版本不可变；修改执行配置会保存为新版本，已有运行仍指向其创建时的版本。</p>
          </div>
          <div class="jd-rev-actions">
            <button
              type="button"
              class="button button-outline jd-secondary"
              :disabled="editorOpen"
              @click="openEditor"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14" /><path d="M5 12h14" /></svg>
              创建新版本
            </button>
            <template v-if="kind === 'collection'">
              <button
                type="button"
                class="button button-primary"
                :disabled="editorOpen || ctx.sourceDisabled.value || detail.selectedRevisionNo.value === null"
                :title="editorOpen ? '正在创建新版本，保存或取消后再执行' : undefined"
                @click="goRun('default')"
              >
                执行
              </button>
              <button type="button" class="button button-outline" disabled title="本期不提供调度">
                调度
              </button>
            </template>
          </div>
        </div>

        <JobConfigEditor
          v-if="editorOpen"
          :key="detail.selectedRevisionNo.value ?? 0"
          :kind="kind"
          :initial-steps="revisionSteps"
          :initial-step-config="detail.selectedRevision.value?.step_config ?? null"
          :saving="editorSaving"
          :error="editorError"
          :workspace-id="workspaceId"
          :db-name="ctx.source.value?.db_name || ''"
          :check-steps="kind === 'publish' ? (check?.steps ?? null) : undefined"
          :checking="checkLoading"
          @cancel="editorOpen = false"
          @save="saveRevision"
          @check="loadCheck"
          @recheck="loadCheck"
        />

        <template v-else>
          <div v-if="detail.selectedRevision.value" class="rev-current">
            <div class="rev-current-head">
              <strong>版本 {{ detail.selectedRevision.value.revision_no }}</strong>
              <span v-if="detail.selectedRevision.value.revision_no === detail.latestRevisionNo.value" class="home-status success">最新</span>
              <span v-else class="home-status running">历史版本</span>
              <span class="rev-current-time">{{ displayDate(detail.selectedRevision.value.created_at) }}</span>
            </div>
            <div class="rev-steps">
              <span v-for="step in detail.selectedRevision.value.steps" :key="step" class="rev-step-chip">
                {{ step }}
              </span>
            </div>
            <p v-if="detail.selectedRevision.value.change_note" class="rev-note">
              变更说明：{{ detail.selectedRevision.value.change_note }}
            </p>
            <p v-if="revisionHasDomainDescription" class="rev-note">
              domain 首次生成输入：{{ revisionDomainDescription }}
            </p>
          </div>

          <div class="rev-list">
            <div
              v-for="revision in [...detail.revisions.value].sort((a, b) => b.revision_no - a.revision_no)"
              :key="revision.revision_no"
              class="rev-row"
              :class="{ current: revision.revision_no === detail.selectedRevisionNo.value }"
            >
              <button
                type="button"
                class="rev-row-main"
                @click="detail.selectRevision(revision.revision_no)"
              >
                <strong>版本 {{ revision.revision_no }}</strong>
                <span class="rev-row-steps">{{ revision.steps.join(' / ') }}</span>
                <span class="rev-row-time">{{ displayDate(revision.created_at) }}</span>
              </button>
              <button
                v-if="kind === 'publish'"
                type="button"
                class="nj-step-edit"
                title="查看 domain / dim_value / rel 参数（工作区配置文件）"
                @click="paramsOpen = true"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" /><circle cx="12" cy="12" r="3" /></svg>
                查看参数
              </button>
            </div>
          </div>

          <!-- 设计稿 34：配置版本卡右下操作行（发布侧；调度本期不提供） -->
          <div v-if="kind === 'publish'" class="jd-rev-footer">
            <button
              type="button"
              class="button button-outline jd-secondary"
              :disabled="checkLoading"
              @click="openCheckCard"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 13c0 5-3.5 7.5-7.7 9a.6.6 0 0 1-.6 0C7.5 20.5 4 18 4 13V6a1 1 0 0 1 .7-1c3-.9 5-1.9 6.7-2.9a1.2 1.2 0 0 1 1.2 0c1.7 1 3.7 2 6.7 2.9a1 1 0 0 1 .7 1Z" /><path d="m9 12 2 2 4-4" /></svg>
              发布校验
            </button>
            <button
              type="button"
              class="button button-primary"
              :disabled="ctx.sourceDisabled.value || detail.selectedRevisionNo.value === null"
              :title="ctx.sourceDisabled.value ? '数据源已禁用，不能发起新的发布运行' : undefined"
              @click="goRun('default')"
            >
              执行
            </button>
          </div>
        </template>
      </section>

      <!-- 发布预检（仅发布作业；按需展开，配置版本卡「发布校验」打开；按当前版本步骤过滤） -->
      <section v-if="kind === 'publish' && checkCardOpen" class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">PUBLISH CHECK</span>
            <h2>发布预检</h2>
            <p>
              按当前查看版本的步骤过滤展示；时间为各输入目录采集完成时间（Asia/Shanghai），
              新旧由你自行判断，系统不自动比较新鲜度。校验结果不拦截执行，执行前的集中预检另做。
            </p>
          </div>
          <div class="jd-check-actions">
            <button type="button" class="ds-refresh" :disabled="checkLoading" @click="loadCheck">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M21 12a9 9 0 0 0-15.5-6.3L3 8" /><path d="M3 3v5h5" />
                <path d="M3 12a9 9 0 0 0 15.5 6.3L21 16" /><path d="M21 21v-5h-5" />
              </svg>
              重新校验
            </button>
            <button type="button" class="ds-new-job-edit" @click="checkCardOpen = false">收起</button>
          </div>
        </div>
        <p v-if="checkLoading" class="ds-jobs-state">正在校验发布预检…</p>
        <p v-else-if="checkError" class="ds-jobs-state error">
          {{ checkError }}
          <button type="button" class="ds-new-job-edit" @click="loadCheck">重试</button>
        </p>
        <template v-else-if="check">
          <div class="pc-rows">
            <div
              v-for="step in PUBLISH_STEPS.filter((name) => revisionSteps.includes(name))"
              :key="step"
              class="pc-row"
            >
              <strong>{{ step }}</strong>
              <span class="home-status" :class="checkByStep.get(step)?.ready ? 'success' : 'warning'">
                {{ checkByStep.get(step)?.ready ? '就绪' : '未就绪' }}
              </span>
              <em
                v-if="!checkByStep.get(step)?.ready && checkByStep.get(step)?.reason"
                class="ds-publish-reason pc-reason"
                :title="checkByStep.get(step)?.reason ?? undefined"
              >
                {{ checkByStep.get(step)?.reason }}
              </em>
              <div class="pc-dirs">
                <span v-for="dir in PUBLISH_STEP_INPUT_DIRS[step] ?? []" :key="dir" class="pc-dir">
                  <em>{{ dir }}/</em>
                  {{ dirCollectedText(dir) }}
                </span>
              </div>
            </div>
          </div>
        </template>
      </section>

      <!-- 运行历史 -->
      <section class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">RUN HISTORY</span>
            <h2>运行历史</h2>
            <p>每次执行生成一条独立运行；点击查看运行详情与逐步骤结果。</p>
          </div>
          <button type="button" class="ds-refresh" :disabled="detail.runsLoading.value" @click="detail.loadRuns">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M21 12a9 9 0 0 0-15.5-6.3L3 8" /><path d="M3 3v5h5" />
              <path d="M3 12a9 9 0 0 0 15.5 6.3L21 16" /><path d="M21 21v-5h-5" />
            </svg>
            刷新
          </button>
        </div>
        <p v-if="detail.runsLoading.value" class="ds-jobs-state">正在读取运行历史…</p>
        <p v-else-if="detail.runsError.value" class="ds-jobs-state error">
          {{ detail.runsError.value }}
          <button type="button" class="ds-new-job-edit" @click="detail.loadRuns">重试</button>
        </p>
        <template v-else-if="detail.runs.value">
          <EmptyState
            v-if="detail.runs.value.items.length === 0"
            title="尚未执行"
            description="该作业还没有运行记录；从上方执行入口发起第一次运行。"
          />
          <div v-else class="run-history">
            <button
              v-for="run in detail.runs.value.items"
              :key="runIdOf(run)"
              type="button"
              class="run-history-row"
              :class="{ current: runIdOf(run) === queryRunId }"
              @click="openHistoryRun(runIdOf(run))"
            >
              <span class="run-history-id">{{ runIdOf(run) }}</span>
              <span class="run-history-rev">版本 {{ run.revision_no }}</span>
              <span class="home-status" :class="run.status">{{ runStatusText(run.status) }}</span>
              <span class="run-history-time">{{ displayDate(run.created_at ?? '') }}</span>
            </button>
          </div>
          <div class="ds-jobs-pager">
            <span>
              共 {{ detail.runs.value.total }} 条记录 · 当前第 {{ detail.runsPage.value }} 页
            </span>
            <div>
              <button
                type="button"
                :disabled="detail.runsPage.value <= 1"
                aria-label="上一页"
                @click="gotoRunsPage(detail.runsPage.value - 1)"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6" /></svg>
              </button>
              <button
                type="button"
                :disabled="detail.runsPage.value * detail.runsPageSize >= detail.runs.value.total"
                aria-label="下一页"
                @click="gotoRunsPage(detail.runsPage.value + 1)"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
              </button>
            </div>
          </div>
        </template>
      </section>
    </template>

    <!-- 作业信息编辑弹窗 -->
    <div v-if="infoOpen" class="dialog-backdrop" @click.self="infoOpen = false">
      <div class="dialog-panel with-close" role="dialog" aria-modal="true" aria-label="编辑作业信息">
        <button type="button" class="dialog-close" aria-label="关闭" @click="infoOpen = false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></button>
        <p>编辑作业信息</p>
        <p class="dialog-note">仅修改名称与说明；执行配置的修改走“保存新版本”。</p>
        <label class="field">
          <span>作业名称</span>
          <input v-model="infoName" type="text" :disabled="infoSaving" />
        </label>
        <label class="field">
          <span>作业说明（可选）</span>
          <input v-model="infoDesc" type="text" :disabled="infoSaving" />
        </label>
        <p v-if="infoError" class="status-message" role="alert">{{ infoError }}</p>
        <div class="dialog-actions">
          <button type="button" class="button button-outline" :disabled="infoSaving" @click="infoOpen = false">取消</button>
          <button type="button" class="button button-primary" :disabled="infoSaving" @click="saveInfo">保存</button>
        </div>
      </div>
    </div>

    <!-- 版本行「查看参数」（发布专属：三 tab 只读展示工作区配置文件） -->
    <RevisionParamsDialog
      v-if="paramsOpen"
      :workspace-id="workspaceId"
      @close="paramsOpen = false"
    />

    <!-- 运行详情抽屉（?run_id= 深链） -->
    <RunDrawer
      v-if="historyRun.runId.value"
      :kind="kind"
      :run="historyRun.detail.value"
      :loading="historyRun.loading.value"
      :error="historyRun.error.value"
      @close="closeHistoryRun"
      @retry="historyRun.retry()"
      @open-step="openStep"
    />

    <!-- 步骤详情抽屉（?step= 深链；不修改作业定义） -->
    <StepDetailDrawer
      v-if="stepDrawerStep && historyRun.runId.value"
      :kind="kind"
      :run="historyRun.detail.value"
      :step-name="stepDrawerStep"
      :loading="historyRun.loading.value"
      :error="historyRun.error.value"
      @close="closeStepDrawer"
      @select-step="switchStep"
    />
  </div>
</template>

<style scoped>
.ws-home-actions {
  display: flex;
  gap: 10px;
}
.ws-home-desc {
  margin: 0;
  color: #66788f;
  font-size: 13px;
}
.jd-rev-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}
.jd-rev-actions .button {
  min-height: 34px;
  font-size: 12px;
}
.jd-secondary svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.rev-current {
  border: 1px solid #e1e9f4;
  border-radius: 12px;
  background: #fcfdff;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.rev-current-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.rev-current-head strong {
  font-size: 14px;
}
.rev-current-time {
  margin-left: auto;
  color: #66788f;
  font-size: 11px;
}
.rev-steps {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.rev-step-chip {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  background: #e3eefb;
  color: #2e7cd6;
  border-radius: 8px;
  padding: 4px 10px;
}
.rev-note {
  margin: 0;
  color: #66788f;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.rev-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.rev-row {
  display: flex;
  align-items: center;
  gap: 10px;
  border: 1px solid #e1e9f4;
  border-radius: 10px;
  background: #fff;
  padding: 0 10px 0 0;
}
.rev-row.current {
  border-color: #2e7cd6;
  background: #eaf3ff;
}
/* 行主体（点击切换查看版本）；「查看参数」等行级操作排在右侧 */
.rev-row-main {
  flex: 1 1 auto;
  min-width: 0;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 14px;
  text-align: left;
  padding: 9px 4px 9px 14px;
  border: 0;
  background: transparent;
  cursor: pointer;
  font: inherit;
  color: inherit;
}
.rev-row-main strong {
  font-size: 12px;
  white-space: nowrap;
}
.rev-row-steps {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  color: #66788f;
  overflow-wrap: anywhere;
}
.rev-row-time {
  color: #66788f;
  font-size: 11px;
  white-space: nowrap;
}
.jd-rev-footer {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 10px;
  border-top: 1px solid #e1e9f4;
  padding-top: 14px;
}
.jd-rev-footer .button {
  min-height: 34px;
  font-size: 12px;
}
.jd-check-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.pc-rows {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
/* 单行左对齐布局（用户 2026-10-26 定案）：步骤/状态/原因/目录依次左排
   不换行，原因与目录超宽时省略号截断，原因悬停可见完整描述 */
.pc-row {
  display: flex;
  align-items: center;
  gap: 10px;
  border: 1px solid #e1e9f4;
  border-radius: 12px;
  background: #fcfdff;
  padding: 11px 14px;
}
.pc-row > strong {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 12px;
  min-width: 88px;
  white-space: nowrap;
}
.pc-reason {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-left: 0;
}
.pc-dirs {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pc-dir {
  color: #66788f;
  font-size: 11px;
  margin-right: 12px;
}
.pc-dir em {
  font-style: normal;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  color: #2e7cd6;
  margin-right: 4px;
}
.run-history {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.run-history-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto auto;
  align-items: center;
  gap: 14px;
  text-align: left;
  padding: 10px 14px;
  border: 1px solid #e1e9f4;
  border-radius: 10px;
  background: #fcfdff;
  cursor: pointer;
}
.run-history-row:hover,
.run-history-row.current {
  border-color: #2e7cd6;
}
.run-history-id {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  overflow-wrap: anywhere;
}
.run-history-rev {
  color: #17293f;
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}
.run-history-time {
  color: #66788f;
  font-size: 11px;
  white-space: nowrap;
}
</style>
