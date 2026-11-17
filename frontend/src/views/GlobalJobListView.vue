<script setup lang="ts">
/**
 * 全局作业列表（采集/发布共用，设计稿 35/37，doc 6.3 §4）：跨数据源与
 * 工作区，不带工作区上下文。与工作区作业列表（WorkspaceJobListView）
 * 是不同页面：本页提供数据源/工作区下拉筛选与分页，按最后一次执行
 * 时间倒序；工作区页自动带入数据源与工作区，保持原样不动。
 * 筛选、关键字与页码均写入 query（source_id/workspace_id/q/page），
 * 刷新可恢复。
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { buildFrom, withFrom } from '../navigation/from'
import { displayDateMs } from '../api/format'
import { ApiError } from '../api/http'
import { listDataSources } from '../api/dataSources'
import type { DataSourceListItem, GlobalJobListResponse, JobKind } from '../api/types'
import { listJobsGlobal, listWorkspaces } from '../api/workspaces'
import type { WorkspaceItem } from '../api/types'
import EmptyState from '../components/EmptyState.vue'
import DeleteResourceDialog from '../components/DeleteResourceDialog.vue'

const props = defineProps<{ kind: JobKind }>()

const route = useRoute()
const router = useRouter()

const kindText = computed(() => (props.kind === 'collection' ? '采集' : '发布'))

// ---- query 状态（source_id / workspace_id / q / page） ----

function queryString(key: string): string {
  const value = Array.isArray(route.query[key]) ? route.query[key][0] : route.query[key]
  return typeof value === 'string' ? value : ''
}

const pageNo = computed(() => {
  const raw = Number(queryString('page'))
  return Number.isInteger(raw) && raw >= 1 ? raw : 1
})

// ---- 数据源与工作区选项（筛选与新建弹窗共用） ----

const sources = ref<DataSourceListItem[]>([])
const workspaces = ref<WorkspaceItem[]>([])
const optionsLoading = ref(false)
const optionsError = ref('')

async function loadOptions() {
  optionsLoading.value = true
  optionsError.value = ''
  try {
    const [sourceResponse, workspaceResponse] = await Promise.all([
      listDataSources(),
      // 后端 limit 上限 100（GET /api/workspaces Query le=100），越界整组 422
      listWorkspaces({ limit: 100 }),
    ])
    sources.value = sourceResponse.items
    workspaces.value = workspaceResponse.items
  } catch (cause) {
    optionsError.value = cause instanceof ApiError ? cause.message : '筛选选项加载失败。'
  } finally {
    optionsLoading.value = false
  }
}

watch(
  () => props.kind,
  (kind) => {
    // 采集/发布两个全局页共用本组件；切换时重置页码、保留筛选条件
    if (kind && pageNo.value > 1) {
      const query = { ...route.query } as Record<string, string>
      delete query.page
      void router.replace({ query })
    }
    void loadOptions()
  },
  { immediate: true },
)

const sourceById = computed(() => {
  const map = new Map<string, DataSourceListItem>()
  for (const source of sources.value) map.set(source.source_id, source)
  return map
})
const workspaceById = computed(() => {
  const map = new Map<string, WorkspaceItem>()
  for (const workspace of workspaces.value) map.set(workspace.workspace_id, workspace)
  return map
})

/** 工作区下拉联动数据源：未选源列出全部，选了源只列该源的工作区 */
const workspaceOptions = computed(() => {
  const sourceId = sourceDraft.value || queryString('source_id')
  if (!sourceId) return workspaces.value
  return workspaces.value.filter((workspace) => workspace.source_id === sourceId)
})

// ---- 筛选卡片：draft → 确认写入 query ----

const sourceDraft = ref(queryString('source_id'))
const workspaceDraft = ref(queryString('workspace_id'))

watch(
  () => [route.query.source_id, route.query.workspace_id] as const,
  ([sourceId, workspaceId]) => {
    sourceDraft.value = typeof sourceId === 'string' ? sourceId : ''
    workspaceDraft.value = typeof workspaceId === 'string' ? workspaceId : ''
  },
  { immediate: true },
)

watch(sourceDraft, () => {
  // 切换数据源后原工作区可能不属于新源，清空草稿待确认
  if (workspaceDraft.value) {
    const workspace = workspaceById.value.get(workspaceDraft.value)
    if (workspace && workspace.source_id !== sourceDraft.value) workspaceDraft.value = ''
  }
})

function applyFilters() {
  const next: Record<string, string> = {}
  if (sourceDraft.value) next.source_id = sourceDraft.value
  if (workspaceDraft.value) next.workspace_id = workspaceDraft.value
  const keyword = queryString('q')
  if (keyword) next.q = keyword
  void router.replace({ query: next })
}

// ---- 关键字搜索（服务端 q；写入 query 并回到第 1 页） ----

const keywordDraft = ref(queryString('q'))
watch(
  () => route.query.q,
  (value) => {
    keywordDraft.value = typeof value === 'string' ? value : ''
  },
  { immediate: true },
)

function applyKeyword() {
  const next: Record<string, string> = {}
  if (sourceDraft.value) next.source_id = sourceDraft.value
  if (workspaceDraft.value) next.workspace_id = workspaceDraft.value
  const keyword = keywordDraft.value.trim()
  if (keyword) next.q = keyword
  void router.replace({ query: next })
}

function clearKeyword() {
  keywordDraft.value = ''
  applyKeyword()
}

// ---- 列表数据 ----

const PAGE_SIZE = 10
const page = ref<GlobalJobListResponse | null>(null)
const deleteTarget = ref<{ job_id: string; job_name: string } | null>(null)
const loading = ref(false)
const error = ref('')

const totalPages = computed(() =>
  page.value ? Math.max(1, Math.ceil(page.value.total / PAGE_SIZE)) : 1,
)

async function loadJobs() {
  loading.value = true
  error.value = ''
  try {
    page.value = await listJobsGlobal(props.kind, {
      sourceId: queryString('source_id') || undefined,
      workspaceId: queryString('workspace_id') || undefined,
      q: queryString('q') || undefined,
      limit: PAGE_SIZE,
      offset: (pageNo.value - 1) * PAGE_SIZE,
    })
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : `${kindText.value}作业列表加载失败。`
  } finally {
    loading.value = false
  }
}

watch(
  () => [
    props.kind,
    route.query.source_id,
    route.query.workspace_id,
    route.query.q,
    route.query.page,
  ] as const,
  () => void loadJobs(),
  { immediate: true },
)

function gotoPage(next: number) {
  if (next < 1 || next > totalPages.value || next === pageNo.value) return
  const query: Record<string, string> = { ...route.query } as Record<string, string>
  if (next > 1) query.page = String(next)
  else delete query.page
  void router.replace({ query })
}

// ---- 新建作业：先选数据源与工作区，再进入对应工作区的新建页（设计稿 36 语义） ----

const createOpen = ref(false)
const createSourceId = ref('')
const createWorkspaceId = ref('')

const createWorkspaceOptions = computed(() =>
  createSourceId.value
    ? workspaces.value.filter((workspace) => workspace.source_id === createSourceId.value)
    : workspaces.value,
)

function openCreate() {
  createSourceId.value = ''
  createWorkspaceId.value = ''
  createOpen.value = true
}

function submitCreate() {
  if (!createWorkspaceId.value) return
  createOpen.value = false
  // doc 9 §7.3 写入点：全局列表的新建作业确认进入新建页，来源为当前列表地址
  const base = `/workspaces/${createWorkspaceId.value}/${props.kind === 'collection' ? 'collection' : 'publish'}/new`
  void router.push(withFrom(base, buildFrom(route.fullPath)))
}

/** doc 9 §7.3 写入点：行内「作业详情」携带当前列表地址（含筛选和页码）作为来源 */
function jobDetailTo(job: { workspace_id: string; job_id: string }) {
  const base = `/workspaces/${job.workspace_id}/${props.kind === 'collection' ? 'collection' : 'publish'}/jobs/${job.job_id}`
  return withFrom(base, buildFrom(route.fullPath))
}
</script>

<template>
  <div class="ds-jobs">
    <header class="ds-jobs-header">
      <div>
        <span class="ds-kicker">{{ kind === 'collection' ? 'GLOBAL COLLECTION JOBS' : 'GLOBAL PUBLISH JOBS' }}</span>
        <h1>{{ kind === 'collection' ? '全部采集作业' : '全部发布作业' }}</h1>
        <p>全部数据源与工作区下的{{ kind === 'collection' ? '数据采集' : '数据发布' }}作业，按最后一次执行时间倒序排列。</p>
      </div>
    </header>

    <!-- doc 9 §9：渲染作业不存在提示（跳转回本列表时携带 notice=not_found） -->
    <p v-if="route.query.notice === 'not_found'" class="ds-banner" role="alert">
      要打开的作业不存在或已删除，已回到全部{{ kind === 'collection' ? '采集' : '发布' }}作业列表。
    </p>

    <!-- 筛选：数据源 → 工作区（联动）→ 确认（设计稿 35/37 DATASCOPE 卡片） -->
    <section class="ds-jobs-card">
      <div class="ds-jobs-list-head">
        <div>
          <span class="ds-kicker ds-kicker-sm">DATASCOPE</span>
          <h2>选择数据源</h2>
        </div>
      </div>
      <p v-if="optionsLoading" class="ds-jobs-state">正在读取筛选选项…</p>
      <p v-else-if="optionsError" class="ds-jobs-state error">
        {{ optionsError }}
        <button type="button" class="ds-new-job-edit" @click="loadOptions">重试</button>
      </p>
      <form v-else class="gj-filters" @submit.prevent="applyFilters">
        <label class="field">
          <span>数据源</span>
          <select v-model="sourceDraft">
            <option value="">全部数据源</option>
            <option v-for="source in sources" :key="source.source_id" :value="source.source_id">
              {{ source.display_name }}（{{ source.source_id }}）
            </option>
          </select>
        </label>
        <span class="gj-filter-arrow" aria-hidden="true">→</span>
        <label class="field">
          <span>工作区</span>
          <select v-model="workspaceDraft">
            <option value="">全部工作区</option>
            <option
              v-for="workspace in workspaceOptions"
              :key="workspace.workspace_id"
              :value="workspace.workspace_id"
            >
              {{ workspace.workspace_name }}（{{ workspace.workspace_id }}）
            </option>
          </select>
        </label>
        <button type="submit" class="button button-primary">确认</button>
      </form>
    </section>

    <!-- 作业列表 -->
    <section class="ds-jobs-card">
      <div class="ds-jobs-list-head">
        <div>
          <span class="ds-kicker ds-kicker-sm">{{ kind === 'collection' ? 'COLLECTION JOBS' : 'PUBLISH JOBS' }}</span>
          <h2>{{ kind === 'collection' ? '数据采集作业列表' : '数据发布作业列表' }}</h2>
        </div>
        <div class="ds-jobs-actions">
          <span v-if="page" class="ds-jobs-count">共 {{ page.total }} 个作业</span>
          <button type="button" class="ds-jobs-create" @click="openCreate">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14" /><path d="M5 12h14" /></svg>
            新建{{ kindText }}作业
          </button>
        </div>
      </div>

      <!-- 关键字搜索（设计稿 35/37 搜索行：浅蓝输入框 + 内嵌图标 + 主色搜索按钮） -->
      <form class="gj-search" @submit.prevent="applyKeyword">
        <div class="gj-search-box">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" /></svg>
          <input v-model="keywordDraft" type="search" placeholder="作业名称 / 作业 ID / 说明" />
        </div>
        <button type="submit" class="button button-primary gj-search-btn">搜索</button>
        <button v-if="queryString('q')" type="button" class="button button-outline gj-search-btn" @click="clearKeyword">
          清除筛选
        </button>
      </form>

      <p v-if="loading" class="ds-jobs-state">正在读取{{ kindText }}作业…</p>
      <p v-else-if="error" class="ds-jobs-state error">
        {{ error }}
        <button type="button" class="ds-new-job-edit" @click="loadJobs">重试</button>
      </p>
      <template v-else-if="page">
        <EmptyState
          v-if="page.items.length === 0"
          title="没有符合条件的作业"
          description="调整数据源/工作区/关键字筛选，或先在工作区内新建作业。"
        />
        <template v-else>
          <div class="gj-table" :class="{ 'gj-table-collection': kind === 'collection' }" role="table">
            <div class="gj-row gj-head" role="row">
              <span>数据源名称</span>
              <span>工作区名称</span>
              <span>作业 ID</span>
              <span>作业名称</span>
              <span>{{ kind === 'collection' ? '执行步骤' : '发布步骤' }}</span>
              <span>最新版本号</span>
              <span>执行次数</span>
              <span>最后一次执行时间</span>
              <span>操作</span>
            </div>
            <div v-for="job in page.items" :key="job.job_id" class="gj-row" role="row">
              <span class="gj-plain" :title="job.source_name">{{ job.source_name }}</span>
              <span class="gj-plain" :title="job.workspace_name">{{ job.workspace_name }}</span>
              <span class="gj-mono">{{ job.job_id }}</span>
              <span class="gj-plain gj-name" :title="job.job_name">{{ job.job_name }}</span>
              <span class="gj-mono gj-steps" :title="job.default_steps.join('/')">
                {{ job.default_steps.join('/') || '—' }}
              </span>
              <span>{{ job.latest_revision_no ?? '—' }}</span>
              <span>{{ job.run_count }}</span>
              <span>{{ job.last_run_at ? displayDateMs(job.last_run_at) : '—' }}</span>
              <div class="gj-row-actions">
                <button type="button" class="ds-danger" @click="deleteTarget = job">删除</button>
                <RouterLink
                  class="ds-manage"
                  :to="jobDetailTo(job)"
                >
                  作业详情
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg>
                </RouterLink>
              </div>
            </div>
          </div>
          <div class="ds-jobs-pager">
            <span>
              共 {{ page.total }} 条记录 · 当前第 {{ pageNo }}/{{ totalPages }} 页
            </span>
            <div>
              <button
                type="button"
                :disabled="pageNo <= 1"
                aria-label="上一页"
                @click="gotoPage(pageNo - 1)"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6" /></svg>
              </button>
              <button
                type="button"
                :disabled="pageNo >= totalPages"
                aria-label="下一页"
                @click="gotoPage(pageNo + 1)"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
              </button>
            </div>
          </div>
        </template>
      </template>
    </section>

    <!-- 新建作业：先选数据源与工作区（设计稿 36 语义），再进入工作区新建页 -->
    <div v-if="createOpen" class="dialog-backdrop" @click.self="createOpen = false">
      <div class="dialog-panel with-close" role="dialog" aria-modal="true" aria-label="新建作业">
        <button type="button" class="dialog-close" aria-label="关闭" @click="createOpen = false">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18" /><path d="m6 6 12 12" /></svg>
        </button>
        <p>新建{{ kindText }}作业</p>
        <p class="dialog-note">
          全局入口不绑定工作区：先选择数据源与工作区，进入该工作区的
          「新建{{ kindText }}作业」页面继续。
        </p>
        <label class="field">
          <span>数据源</span>
          <select v-model="createSourceId" class="gj-create-select">
            <option value="">请选择数据源</option>
            <option v-for="source in sources" :key="source.source_id" :value="source.source_id">
              {{ source.display_name }}（{{ source.source_id }}）
            </option>
          </select>
        </label>
        <label class="field gj-create-gap">
          <span>工作区</span>
          <select v-model="createWorkspaceId" class="gj-create-select" :disabled="!createSourceId">
            <option value="">请选择工作区</option>
            <option
              v-for="workspace in createWorkspaceOptions"
              :key="workspace.workspace_id"
              :value="workspace.workspace_id"
            >
              {{ workspace.workspace_name }}（{{ workspace.workspace_id }}）
            </option>
          </select>
        </label>
        <div class="dialog-actions gj-create-actions">
          <button type="button" class="button button-outline" @click="createOpen = false">取消</button>
          <button type="button" class="button button-primary" :disabled="!createWorkspaceId" @click="submitCreate">
            进入新建页
          </button>
        </div>
      </div>
    </div>
  </div>
  <DeleteResourceDialog
    v-if="deleteTarget"
    kind="job"
    :job-kind="kind"
    :target-id="deleteTarget.job_id"
    :display-name="deleteTarget.job_name"
    @cancel="deleteTarget = null"
    @done="deleteTarget = null; loadJobs()"
  />
</template>

<style scoped>
.gj-filters {
  display: flex;
  align-items: end;
  /* 行间距大于列间距：换行后「工作区」标签与上一行下拉保持呼吸感 */
  gap: 20px 28px;
  flex-wrap: wrap;
}
.gj-filters .field {
  margin: 0;
  width: min(280px, 100%);
}
.gj-filters .field > span:first-child {
  display: block;
  color: #66788f;
  font-size: 11px;
  font-weight: 700;
  margin-bottom: 7px;
}
.gj-filters select {
  width: 100%;
  font-size: 12px;
  padding: 8px 12px;
  border: 1px solid #d9e3ef;
  border-radius: 10px;
  background: #fff;
  color: #20364f;
  outline: none;
}
.gj-filters select:focus {
  border-color: #3a83dc;
  box-shadow: 0 0 0 3px #dbeeff;
}
.gj-filter-arrow {
  align-self: center;
  color: #8aa0b8;
  font-weight: 700;
  padding-bottom: 2px;
}
/* 关键字搜索（设计稿 35/37 搜索行） */
.gj-search {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.gj-search-box {
  display: flex;
  align-items: center;
  gap: 8px;
  width: min(400px, 100%);
  background: #eef3f9;
  border: 1px solid #d9e3ef;
  border-radius: 10px;
  padding: 10px 14px;
}
.gj-search-box svg {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  fill: none;
  stroke: #66788f;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.gj-search-box input {
  flex: 1;
  min-width: 0;
  border: 0;
  background: transparent;
  outline: none;
  font-size: 12px;
  color: #20364f;
}
.gj-search-box input::placeholder {
  color: #66788f;
}
.gj-search-btn {
  min-height: 38px;
  font-size: 12px;
  border-radius: 10px;
  padding: 0 20px;
}
.gj-filters .button {
  min-height: 34px;
  font-size: 12px;
}
/* 表格（设计稿 35/37 九列） */
.gj-table {
  display: flex;
  flex-direction: column;
  gap: 6px;
  overflow-x: auto;
}
.gj-row {
  display: grid;
  /* 表头与数据共用比例列；步骤列预留约三分之一的空间；
     操作列按「删除 + 作业详情」两按钮同行预留。 */
  grid-template-columns:
    minmax(90px, 9fr) minmax(90px, 9fr) minmax(100px, 9fr)
    minmax(110px, 12fr) minmax(300px, 34fr)
    minmax(65px, 5fr) minmax(55px, 5fr) minmax(150px, 11fr)
    minmax(140px, 6fr);
  min-width: 1200px;
  align-items: center;
  column-gap: 16px;
  padding: 10px 12px;
  border: 1px solid #e1e9f4;
  border-radius: 10px;
  background: #fcfdff;
  font-size: 12px;
}
/* 采集列表按参考图分配九列，执行步骤保留最多空间。 */
.gj-table-collection .gj-row {
  grid-template-columns:
    minmax(90px, 9fr) minmax(90px, 9fr) minmax(100px, 9fr)
    minmax(110px, 12fr) minmax(300px, 33fr)
    minmax(65px, 5.5fr) minmax(55px, 5.5fr) minmax(150px, 11fr)
    minmax(140px, 6fr);
}
.gj-row-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}
.gj-head {
  background: transparent;
  border-color: transparent;
  padding-top: 0;
  padding-bottom: 0;
  color: #66788f;
  font-size: 11px;
  font-weight: 700;
}
.gj-row > span,
.gj-row > a {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.gj-plain {
  color: #17293f;
}
.gj-mono {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  color: #2e7cd6;
}
.gj-row > .gj-steps {
  color: #66788f;
  white-space: normal;
  overflow-wrap: anywhere;
  line-height: 1.6;
}
.gj-name {
  font-weight: 700;
}
.gj-row .ds-manage {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}
.gj-row .ds-manage svg {
  width: 11px;
  height: 11px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
/* 新建作业弹窗：下拉字体 10px；「工作区」标签与上方下拉拉开间距；按钮组下移 */
.gj-create-select {
  font-size: 12px;
  padding: 6px 9px;
}
.gj-create-gap {
  margin-top: 16px;
}
.gj-create-actions {
  margin-top: 18px;
}
</style>
