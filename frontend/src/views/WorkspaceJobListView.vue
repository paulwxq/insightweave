<script setup lang="ts">
/**
 * 工作区作业列表（采集/发布共用；doc 6.2 §9.1：已保存作业与执行历史分开）。
 * 作业列表接口返回该工作区全部作业（无分页信封，见 doc 6.2 §6.1），
 * 因此本页不做分页；关键字筛选在客户端完成并写入 query，刷新可恢复。
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { displayDateMs } from '../api/format'
import { ApiError } from '../api/http'
import type { JobItem, JobKind } from '../api/types'
import { listJobs } from '../api/workspaces'
import EmptyState from '../components/EmptyState.vue'
import { useWorkspaceContext } from '../composables/useWorkspaceContext'

const props = defineProps<{ kind: JobKind }>()

const route = useRoute()
const router = useRouter()
const workspaceId = computed(() =>
  typeof route.params.workspaceId === 'string' ? route.params.workspaceId : '',
)
const { workspace, source, loading: ctxLoading, error: ctxError } = useWorkspaceContext(
  () => workspaceId.value,
)

const DB_TYPE_LABEL: Record<string, string> = {
  postgresql: 'PostgreSQL',
  mysql: 'MySQL',
  sqlite: 'SQLite',
  csv: 'CSV',
  excel: 'Excel',
}

function sourceTypeText(): string {
  if (!source.value) return '—'
  const label = DB_TYPE_LABEL[source.value.db_type] ?? source.value.db_type
  return source.value.db_version ? `${label} ${source.value.db_version}` : label
}

const jobs = ref<JobItem[]>([])
const jobsLoading = ref(false)
const jobsError = ref('')

const kindText = computed(() => (props.kind === 'collection' ? '采集' : '发布'))
const basePath = computed(
  () => `/workspaces/${workspaceId.value}/${props.kind === 'collection' ? 'collection' : 'publish'}`,
)

// ---- 关键字筛选（全量列表客户端过滤；写入 query） ----

const keywordQuery = computed(() => {
  const value = Array.isArray(route.query.q) ? route.query.q[0] : route.query.q
  return typeof value === 'string' ? value.trim() : ''
})
const keywordDraft = ref('')
watch(keywordQuery, (value) => {
  keywordDraft.value = value
}, { immediate: true })

const filteredJobs = computed(() => {
  const keyword = keywordQuery.value.toLowerCase()
  if (keyword === '') return jobs.value
  return jobs.value.filter((job) =>
    [job.job_name, job.job_id, job.job_desc]
      .filter((field): field is string => field !== null)
      .some((field) => field.toLowerCase().includes(keyword)),
  )
})

function applyKeyword() {
  const next = { ...route.query }
  const keyword = keywordDraft.value.trim()
  if (keyword) next.q = keyword
  else delete next.q
  void router.replace({ query: next })
}

function clearKeyword() {
  keywordDraft.value = ''
  applyKeyword()
}

async function loadJobs() {
  jobsLoading.value = true
  jobsError.value = ''
  try {
    const response = await listJobs(props.kind, workspaceId.value)
    jobs.value = response.items
  } catch (cause) {
    jobsError.value =
      cause instanceof ApiError ? cause.message : `${kindText.value}作业列表加载失败。`
  } finally {
    jobsLoading.value = false
  }
}

watch(workspaceId, () => void loadJobs(), { immediate: true })
</script>

<template>
  <div class="ds-jobs">
    <header class="ds-jobs-header">
      <div>
        <span class="ds-kicker">{{ kind === 'collection' ? 'COLLECTION JOBS' : 'PUBLISH JOBS' }}</span>
        <h1>{{ kind === 'collection' ? '数据采集作业' : '数据发布作业' }}</h1>
        <p>
          当前工作区下的全部{{ kind === 'collection' ? '数据采集' : '数据发布' }}作业。
        </p>
      </div>
      <div class="ws-home-actions">
        <RouterLink class="ds-jobs-back" :to="`/workspaces/${workspaceId}`">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
          返回
        </RouterLink>
      </div>
    </header>

    <p v-if="route.query.notice === 'not_found'" class="ds-banner" role="alert">
      作业不存在或不属于该工作区
    </p>

    <p v-if="ctxLoading" class="ds-jobs-state">正在读取工作区…</p>
    <p v-else-if="ctxError" class="ds-jobs-state error">{{ ctxError }}</p>

    <template v-else-if="workspace && source">
      <section class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">WORKSPACE INFO</span>
            <h2>数据源基本信息</h2>
          </div>
        </div>
        <div class="ds-jobs-facts ws-source-facts">
          <p><span>工作区名称</span><strong class="ws-plain">{{ workspace.workspace_name }}</strong></p>
          <p><span>数据源名称</span><strong class="ws-plain">{{ source.display_name }}</strong></p>
          <p><span>数据库名称</span><strong>{{ source.db_name }}</strong></p>
          <p><span>HOST : PORT</span><strong>{{ source.host }}:{{ source.port }}</strong></p>
          <p><span>数据库类型 / 版本</span><strong class="ws-plain">{{ sourceTypeText() }}</strong></p>
        </div>
      </section>

      <section class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">WORKSPACE JOBS</span>
            <h2>{{ kind === 'collection' ? '数据采集作业列表' : '数据发布作业列表' }}</h2>
          </div>
          <div class="ds-jobs-actions">
            <span class="ds-jobs-count">
              共 {{ jobs.length }} 个作业<template v-if="keywordQuery">，符合 {{ filteredJobs.length }} 个</template>
            </span>
            <RouterLink class="ds-jobs-create" :to="`${basePath}/new`">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14" /><path d="M5 12h14" /></svg>
              新建{{ kindText }}作业
            </RouterLink>
          </div>
        </div>

        <form v-if="jobs.length > 0" class="ws-jobs-filter" @submit.prevent="applyKeyword">
          <label class="field ws-jobs-filter-keyword">
            <span>关键字搜索</span>
            <input v-model="keywordDraft" type="search" placeholder="作业名称 / 作业 ID / 说明" />
          </label>
          <button type="submit" class="button button-outline">搜索</button>
          <button v-if="keywordQuery" type="button" class="button button-outline" @click="clearKeyword">
            清除筛选
          </button>
        </form>

        <p v-if="jobsLoading" class="ds-jobs-state">正在读取{{ kindText }}作业…</p>
        <p v-else-if="jobsError" class="ds-jobs-state error">
          {{ jobsError }}
          <button type="button" class="ds-new-job-edit" @click="loadJobs">重试</button>
        </p>
        <template v-else>
          <EmptyState
            v-if="jobs.length === 0"
            :title="`该工作区还没有${kindText}作业`"
            :description="`新建一个${kindText}作业，保存可复用的步骤与默认参数；保存不会执行任何步骤。`"
          >
            <template #action>
              <RouterLink class="button button-outline" :to="`${basePath}/new`">
                新建{{ kindText }}作业
              </RouterLink>
            </template>
          </EmptyState>
          <EmptyState
            v-else-if="filteredJobs.length === 0"
            title="没有符合条件的作业"
            description="换个关键字试试。"
          >
            <template #action>
              <button type="button" class="button button-outline" @click="clearKeyword">清除筛选</button>
            </template>
          </EmptyState>
          <div v-else class="ws-job-list">
            <div v-for="job in filteredJobs" :key="job.job_id" class="ws-job-row">
              <div class="ws-job-main">
                <div class="ws-job-title">
                  <strong>{{ job.job_name }}</strong>
                  <span class="ws-job-id">{{ job.job_id }}</span>
                </div>
                <span v-if="job.job_desc" class="ws-job-desc">{{ job.job_desc }}</span>
                <span class="ws-job-steps">默认步骤 {{ job.default_steps.join(' / ') || '—' }}</span>
              </div>
              <div class="ws-job-meta">
                <span>执行 {{ job.run_count }} 次</span>
                <span>版本 {{ job.latest_revision_no ?? '—' }}</span>
                <span>更新时间 {{ displayDateMs(job.updated_at) }}</span>
              </div>
              <RouterLink class="ds-manage" :to="`${basePath}/jobs/${job.job_id}`">
                作业详情
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg>
              </RouterLink>
            </div>
          </div>
        </template>
      </section>
    </template>
  </div>
</template>

<style scoped>
.ds-jobs .ds-jobs-header {
  align-items: flex-end;
}
.ws-home-actions {
  display: flex;
  gap: 10px;
}
.ws-source-facts {
  grid-template-columns: repeat(5, minmax(0, 1fr));
}
.ws-plain {
  font-family: "Noto Sans SC", system-ui, sans-serif;
}
.ws-jobs-filter {
  display: flex;
  align-items: end;
  gap: 12px;
  flex-wrap: wrap;
}
.ws-jobs-filter .field {
  margin: 0;
}
.ws-jobs-filter-keyword {
  width: min(320px, 100%);
}
.ws-jobs-filter-keyword input {
  font-size: 12px;
  padding: 8px 12px;
}
.ws-jobs-filter .field > span:first-child {
  display: block;
  color: #66788f;
  font-size: 11px;
  font-weight: 700;
  margin-bottom: 5px;
}
.ws-jobs-filter .button {
  min-height: 34px;
  font-size: 12px;
}
.ws-job-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ws-job-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 16px;
  padding: 12px 16px;
  border: 1px solid #e1e9f4;
  border-radius: 12px;
  background: #fcfdff;
}
.ws-job-main {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.ws-job-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}
.ws-job-title strong {
  font-size: 14px;
}
.ws-job-id {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  color: #2e7cd6;
}
.ws-job-desc {
  color: #66788f;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.ws-job-steps {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  color: #66788f;
  overflow-wrap: anywhere;
}
.ws-job-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  flex-shrink: 0;
}
.ws-job-meta span {
  color: #66788f;
  font-size: 12px;
  white-space: nowrap;
  padding: 0 14px;
}
.ws-job-meta span:first-child {
  padding-left: 0;
}
.ws-job-meta span + span {
  border-left: 1px solid #d9e3ef;
}
.ws-job-row .ds-manage {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}
.ws-job-row .ds-manage svg {
  width: 11px;
  height: 11px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
@media (max-width: 1100px) {
  .ws-source-facts {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 800px) {
  .ws-source-facts,
  .ws-job-row {
    grid-template-columns: 1fr;
    justify-items: start;
  }
}
</style>
