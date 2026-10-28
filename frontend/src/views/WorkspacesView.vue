<script setup lang="ts">
/**
 * 工作区管理列表（doc 6.3 §4 编号 21）：跨源列表 + 服务端分页。
 *
 * 数据来自 `GET /api/workspaces?source_id=&limit=&offset=`（doc 6.2 §6.1
 * 分页信封 items/total/limit/offset），禁止前端逐数据源拉取后拼接分页。
 * 筛选（source_id）与分页（page）状态写入 query，刷新可恢复。
 * 列表行带每工作区采集/发布作业数量（doc 6.2 §6.1 列表行聚合），
 * 直接展示为两列计数；创建时间用 displayDateMs 毫秒口径（三位小数）。
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listDataSources } from '../api/dataSources'
import { displayDateMs } from '../api/format'
import { ApiError } from '../api/http'
import type { DataSourceListItem, WorkspacePageResponse } from '../api/types'
import { listWorkspaces } from '../api/workspaces'
import EmptyState from '../components/EmptyState.vue'

const PAGE_SIZE = 20

const route = useRoute()
const router = useRouter()

const sources = ref<DataSourceListItem[]>([])
const sourcesLoading = ref(false)
const sourcesError = ref('')

const page = ref<WorkspacePageResponse | null>(null)
const loading = ref(false)
const error = ref('')
/** 接口 404（非业务错误码）时提示后端缺口，而非笼统失败文案 */
const endpointMissing = ref(false)

const sourceId = computed(() => {
  const value = Array.isArray(route.query.source_id) ? route.query.source_id[0] : route.query.source_id
  return typeof value === 'string' && value !== '' ? value : ''
})
const typeQuery = computed(() => {
  const value = Array.isArray(route.query.db_type) ? route.query.db_type[0] : route.query.db_type
  return typeof value === 'string' ? value.trim() : ''
})
const pageNo = computed(() => {
  const raw = Number(Array.isArray(route.query.page) ? route.query.page[0] : route.query.page)
  return Number.isInteger(raw) && raw >= 1 ? raw : 1
})
const offset = computed(() => (pageNo.value - 1) * PAGE_SIZE)
const totalPages = computed(() => Math.max(1, Math.ceil((page.value?.total ?? 0) / PAGE_SIZE)))

const sourceById = computed(() => {
  const map = new Map<string, DataSourceListItem>()
  for (const item of sources.value) map.set(item.source_id, item)
  return map
})

async function loadSources() {
  sourcesLoading.value = true
  sourcesError.value = ''
  try {
    const response = await listDataSources()
    sources.value = response.items
  } catch (cause) {
    sourcesError.value = cause instanceof Error ? cause.message : '数据源列表加载失败。'
  } finally {
    sourcesLoading.value = false
  }
}

async function load() {
  loading.value = true
  error.value = ''
  endpointMissing.value = false
  try {
    page.value = await listWorkspaces({
      source_id: sourceId.value || undefined,
      limit: PAGE_SIZE,
      offset: offset.value,
    })
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 404 && cause.code === 'internal_error') {
      endpointMissing.value = true
      error.value = '跨源工作区列表接口暂不可用。'
    } else {
      error.value = cause instanceof Error ? cause.message : '工作区列表加载失败。'
    }
  } finally {
    loading.value = false
  }
}

const DB_TYPE_LABELS: Record<string, string> = {
  postgresql: 'PostgreSQL',
  mysql: 'MySQL',
  sqlite: 'SQLite',
  csv: 'CSV',
  excel: 'Excel',
}

function dbTypeLabel(value: string): string {
  return DB_TYPE_LABELS[value.toLowerCase()] ?? value
}

/** 已登记数据源里实际出现过的类型。 */
const dbTypes = computed(() => {
  const seen = new Map<string, string>()
  for (const item of sources.value) {
    const raw = item.db_type.trim()
    if (raw === '') continue
    const key = raw.toLowerCase()
    if (!seen.has(key)) seen.set(key, raw)
  }
  return [...seen.values()].sort((left, right) =>
    dbTypeLabel(left).localeCompare(dbTypeLabel(right), 'zh'),
  )
})

function canonicalType(value: string): string {
  if (value === '') return ''
  return dbTypes.value.find((item) => item.toLowerCase() === value.toLowerCase()) ?? value
}

const typeDraft = ref('')
const nameDraft = ref('')

const nameOptions = computed(() => {
  const type = typeDraft.value.toLowerCase()
  return sources.value
    .filter((item) => type === '' || item.db_type.toLowerCase() === type)
    .slice()
    .sort((left, right) =>
      (left.display_name || left.db_name).localeCompare(right.display_name || right.db_name, 'zh'),
    )
})

function syncDraftsFromRoute() {
  const source = sourceId.value ? sourceById.value.get(sourceId.value) : undefined
  if (source) {
    typeDraft.value = canonicalType(source.db_type)
    nameDraft.value = source.source_id
    return
  }
  typeDraft.value = canonicalType(typeQuery.value)
  nameDraft.value = ''
}

function onTypeDraftChange() {
  if (nameDraft.value === '') return
  const source = sourceById.value.get(nameDraft.value)
  const type = typeDraft.value.toLowerCase()
  if (!source || (type !== '' && source.db_type.toLowerCase() !== type)) {
    nameDraft.value = ''
  }
}

function writeQuery(next: { source_id?: string; db_type?: string; page?: number }) {
  const query: Record<string, string> = {}
  const nextSource = next.source_id !== undefined ? next.source_id : sourceId.value
  const nextType = next.db_type !== undefined ? next.db_type : typeQuery.value
  const nextPage = next.page !== undefined ? next.page : 1
  if (nextType) query.db_type = nextType
  if (nextSource) query.source_id = nextSource
  if (nextPage > 1) query.page = String(nextPage)
  void router.replace({ path: '/workspaces', query })
}

function applySearch() {
  writeQuery({ source_id: nameDraft.value, db_type: typeDraft.value, page: 1 })
}

function prevPage() {
  if (pageNo.value > 1) writeQuery({ page: pageNo.value - 1 })
}

function nextPage() {
  if (pageNo.value < totalPages.value) writeQuery({ page: pageNo.value + 1 })
}

watch(
  () => [sourceId.value, typeQuery.value, sources.value.length],
  () => syncDraftsFromRoute(),
  { immediate: true },
)
watch(
  () => [sourceId.value, pageNo.value],
  () => void load(),
  { immediate: true },
)
void loadSources()
</script>

<template>
  <div class="ws-list-page">
    <header class="ds-jobs-header">
      <div>
        <span class="ds-kicker">WORKSPACE MANAGEMENT</span>
        <h1>工作区管理</h1>
        <p>
          跨数据源管理全部工作区；一个工作区隔离一份文件产物，采集与发布作业保存在工作区内。
        </p>
      </div>
      <div class="ws-list-actions">
        <RouterLink class="ds-jobs-back" to="/data-sources">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
          返回
        </RouterLink>
        <RouterLink
          class="ds-jobs-create"
          :to="sourceId ? `/workspaces/new?source_id=${sourceId}` : '/workspaces/new'"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14" /><path d="M5 12h14" /></svg>
          新建工作区
        </RouterLink>
      </div>
    </header>

    <p v-if="route.query.notice === 'not_found'" class="state-block error-state" role="alert">
      工作区不存在或已被删除
    </p>

    <section class="ds-jobs-card">
      <div class="ds-jobs-list-head">
        <div>
          <span class="ds-kicker ds-kicker-sm">WORKSPACES</span>
          <h2>工作区列表</h2>
          <p>按创建时间倒序；先选数据库类型，再选该类型下的数据库名称。</p>
        </div>
      </div>
      <form class="ws-filter" @submit.prevent="applySearch">
        <label class="field ws-filter-field ws-filter-type">
          <span>数据库类型</span>
          <select v-model="typeDraft" :disabled="sourcesLoading" @change="onTypeDraftChange">
            <option value="">全部</option>
            <option v-for="type in dbTypes" :key="type" :value="type">{{ dbTypeLabel(type) }}</option>
          </select>
        </label>
        <label class="field ws-filter-field ws-filter-name">
          <span>数据库名称</span>
          <select v-model="nameDraft" :disabled="sourcesLoading">
            <option value="">全部</option>
            <option v-for="item in nameOptions" :key="item.source_id" :value="item.source_id">
              {{ item.display_name || item.db_name }}
            </option>
          </select>
        </label>
        <button type="submit" class="button button-outline" :disabled="sourcesLoading">搜索</button>
      </form>

      <p v-if="sourcesError" class="ds-jobs-state">
        数据源筛选项加载失败：{{ sourcesError }}
        <button type="button" class="ds-new-job-edit" @click="loadSources">重试</button>
      </p>

      <p v-if="loading" class="ds-jobs-state">正在读取工作区列表…</p>
      <div v-else-if="error" class="state-block error-state" role="alert">
        <p>{{ error }}</p>
        <p v-if="endpointMissing" class="ws-endpoint-hint">
          跨源列表依赖服务端分页接口 GET /api/workspaces（doc 6.2 §6.1）；
          若持续失败，请确认后端已提供该接口。本页不做逐数据源拼接兜底。
        </p>
        <button type="button" class="button button-outline" @click="load">重试</button>
      </div>
      <template v-else-if="page">
        <EmptyState
          v-if="page.items.length === 0"
          :title="sourceId ? '该数据源还没有工作区' : '还没有工作区'"
          :description="
            sourceId
              ? '为该数据源创建工作区后，可在其中保存采集与发布作业。'
              : '选择数据源并创建工作区，开始采集与发布作业。'
          "
        >
          <template #action>
            <RouterLink
              class="button button-primary"
              :to="sourceId ? `/workspaces/new?source_id=${sourceId}` : '/workspaces/new'"
            >
              新建工作区
            </RouterLink>
          </template>
        </EmptyState>
        <template v-else>
          <div class="ws-table" role="table" aria-label="工作区列表">
            <div class="ws-table-row ws-table-head" role="row">
              <span role="columnheader">工作区</span>
              <span role="columnheader">数据源</span>
              <span role="columnheader">说明</span>
              <span role="columnheader">采集作业数量</span>
              <span role="columnheader">发布作业数量</span>
              <span role="columnheader">创建时间</span>
              <span role="columnheader"></span>
            </div>
            <div v-for="item in page.items" :key="item.workspace_id" class="ws-table-row" role="row">
              <span class="ws-table-main" role="cell">
                <strong>{{ item.workspace_name }}</strong>
                <small>{{ item.workspace_id }}</small>
              </span>
              <span class="ws-table-source" role="cell">
                <strong>{{ sourceById.get(item.source_id)?.display_name ?? '—' }}</strong>
                <small>{{ item.source_id }}</small>
              </span>
              <span class="ws-table-desc" role="cell">{{ item.workspace_desc || '—' }}</span>
              <span class="ws-table-count" role="cell">{{ item.collection_job_count }}</span>
              <span class="ws-table-count" role="cell">{{ item.publish_job_count }}</span>
              <span class="ws-table-time" role="cell">{{ displayDateMs(item.created_at) }}</span>
              <span role="cell">
                <RouterLink class="ds-manage" :to="`/workspaces/${item.workspace_id}`">
                  进入工作区
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg>
                </RouterLink>
              </span>
            </div>
          </div>
          <div class="ds-jobs-pager">
            <span>
              共 {{ page.total }} 个工作区 · 当前第 {{ pageNo }} / {{ totalPages }} 页
            </span>
            <div>
              <button type="button" :disabled="pageNo <= 1" aria-label="上一页" @click="prevPage">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6" /></svg>
              </button>
              <button
                type="button"
                :disabled="pageNo >= totalPages"
                aria-label="下一页"
                @click="nextPage"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
              </button>
            </div>
          </div>
        </template>
      </template>
    </section>
  </div>
</template>

<style scoped>
.ws-list-page {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.ws-list-page .ds-jobs-header {
  align-items: center;
}
.ws-list-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.ws-filter {
  display: flex;
  align-items: end;
  gap: 12px;
  flex-wrap: wrap;
  margin: 0;
}
.ws-filter-field {
  margin: 0;
}
.ws-filter-type {
  width: 128px;
}
.ws-filter-name {
  width: 168px;
}
.ws-filter .field > span:first-child {
  display: block;
  color: #66788f;
  font-size: 11px;
  font-weight: 700;
  margin-bottom: 5px;
}
.ws-filter .field select {
  width: 100%;
  height: 32px;
  padding: 0 10px;
  font-size: 12px;
  line-height: 32px;
}
.ws-filter .button {
  min-height: 32px;
  height: 32px;
  padding: 0 12px;
  font-size: 12px;
}
.ws-endpoint-hint {
  color: #8a6d3b;
  font-size: 12px;
  max-width: 560px;
  margin: 0 auto 6px;
}
.ws-table {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
/*
 * 尾部四列（采集数、发布数、创建时间、操作）固定宽度：每行是独立 grid，
 * auto 列会随本行内容伸缩导致表头与数据行错位；固定后各行轨道一致。
 * 前三列按 fr 比例分配剩余宽度，说明列（长文本）占大头。
 */
.ws-table-row {
  display: grid;
  grid-template-columns:
    minmax(150px, 1fr) minmax(130px, 1fr) minmax(160px, 1.8fr)
    76px 76px 148px 88px;
  align-items: center;
  gap: 14px;
  padding: 11px 14px;
  border: 1px solid #e1e9f4;
  border-radius: 12px;
  background: #fcfdff;
}
.ws-table-head {
  border: 0;
  background: transparent;
  padding-top: 0;
  padding-bottom: 0;
  color: #66788f;
  font-size: 11px;
  font-weight: 700;
}
.ws-table-head span {
  white-space: nowrap;
}
.ws-table-main,
.ws-table-source {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.ws-table-main strong,
.ws-table-source strong {
  font-size: 13px;
  color: #17293f;
  overflow-wrap: anywhere;
}
.ws-table-main small,
.ws-table-source small {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 10px;
  color: #2e7cd6;
  overflow-wrap: anywhere;
}
.ws-table-desc {
  color: #66788f;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.ws-table-count {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 12px;
  color: #17293f;
  white-space: nowrap;
}
.ws-table-time {
  color: #66788f;
  font-size: 11px;
  white-space: nowrap;
}
.ws-table-row .ds-manage {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}
.ws-table-row .ds-manage svg {
  width: 11px;
  height: 11px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
@media (max-width: 1360px) {
  .ws-table-row {
    grid-template-columns: 1fr 1fr;
  }
  .ws-table-head {
    display: none;
  }
}
</style>
