<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listDataSources } from '../api/dataSources'
import { displayDate } from '../api/format'
import type { DataSourceListItem } from '../api/types'
import EmptyState from '../components/EmptyState.vue'

const route = useRoute()
const router = useRouter()
const sources = ref<DataSourceListItem[]>([])
const loading = ref(true)
const error = ref('')
const countsReady = computed(() => !loading.value && !error.value)
const registeredCount = computed(() => (countsReady.value ? String(sources.value.length) : '—'))
const availableCount = computed(() =>
  countsReady.value ? String(sources.value.filter((item) => item.is_active).length) : '—',
)
const disabledCount = computed(() =>
  countsReady.value ? String(sources.value.filter((item) => !item.is_active).length) : '—',
)

// ---- 筛选（列表接口返回全部数据源，客户端过滤；状态写入 query 可恢复） ----

type StatusFilter = 'all' | 'active' | 'disabled'

const statusFilter = computed<StatusFilter>(() => {
  const value = Array.isArray(route.query.status) ? route.query.status[0] : route.query.status
  return value === 'active' || value === 'disabled' ? value : 'all'
})
const keywordQuery = computed(() => {
  const value = Array.isArray(route.query.q) ? route.query.q[0] : route.query.q
  return typeof value === 'string' ? value.trim() : ''
})
const keywordDraft = ref(keywordQuery.value)

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

/** 选项只来自当前已登记数据源的 db_type，不预置未出现的类型。 */
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

const dbTypeFilter = computed(() => {
  const value = Array.isArray(route.query.db_type) ? route.query.db_type[0] : route.query.db_type
  if (typeof value !== 'string' || value.trim() === '') return ''
  return dbTypes.value.find((item) => item.toLowerCase() === value.trim().toLowerCase()) ?? ''
})

const filteredSources = computed(() => {
  const keyword = keywordQuery.value.toLowerCase()
  const dbType = dbTypeFilter.value.toLowerCase()
  return sources.value.filter((item) => {
    if (statusFilter.value === 'active' && !item.is_active) return false
    if (statusFilter.value === 'disabled' && item.is_active) return false
    if (dbType !== '' && item.db_type.toLowerCase() !== dbType) return false
    if (keyword === '') return true
    return [item.display_name, item.source_id, item.db_name, item.host]
      .filter(Boolean)
      .some((field) => field.toLowerCase().includes(keyword))
  })
})

const hasFilter = computed(
  () => statusFilter.value !== 'all' || keywordQuery.value !== '' || dbTypeFilter.value !== '',
)

function writeFilterQuery(next: { status?: StatusFilter; q?: string; db_type?: string }) {
  const query: Record<string, string> = {}
  const status = next.status !== undefined ? next.status : statusFilter.value
  const keyword = (next.q !== undefined ? next.q : keywordQuery.value).trim()
  const dbType = next.db_type !== undefined ? next.db_type : dbTypeFilter.value
  if (status !== 'all') query.status = status
  if (dbType) query.db_type = dbType
  if (keyword) query.q = keyword
  void router.replace({ path: '/data-sources', query })
}

function onStatusChange(value: string) {
  const next = value === 'active' || value === 'disabled' ? value : 'all'
  if (next === statusFilter.value) return
  writeFilterQuery({ status: next })
}

function onDbTypeChange(value: string) {
  const next = dbTypes.value.find((item) => item === value) ?? ''
  if (next === dbTypeFilter.value) return
  writeFilterQuery({ db_type: next })
}

function applyKeyword() {
  if (keywordDraft.value.trim() === keywordQuery.value) return
  writeFilterQuery({ q: keywordDraft.value })
}

function clearFilter() {
  keywordDraft.value = ''
  writeFilterQuery({ status: 'all', q: '', db_type: '' })
}

async function reload() {
  loading.value = true
  error.value = ''
  try {
    const response = await listDataSources()
    sources.value = response.items
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '数据源列表加载失败。'
  } finally {
    loading.value = false
  }
}

onMounted(reload)
</script>

<template>
  <div class="ds-page">
    <header class="ds-header">
      <div class="ds-title">
        <span class="ds-kicker">DATA SOURCE</span>
        <h1>数据源管理</h1>
        <p>登记并管理数据源连接。登记仅表示接入系统，完成画像采集后，数据才会进入分析项目。</p>
      </div>
      <RouterLink to="/data-sources/new" class="ds-create">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <ellipse cx="12" cy="5" rx="9" ry="3" />
          <path d="M3 5v14a9 3 0 0 0 18 0V5" />
          <path d="M3 12a9 3 0 0 0 18 0" />
        </svg>
        新建数据源连接
      </RouterLink>
    </header>

    <section class="ds-metrics">
      <article class="ds-metric">
        <div class="ds-metric-main">
          <span class="ds-metric-icon ds-metric-icon-blue">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <rect width="20" height="8" x="2" y="2" rx="2" />
              <rect width="20" height="8" x="2" y="14" rx="2" />
              <path d="M6 6h.01M6 18h.01" />
            </svg>
          </span>
          <div>
            <small>已登记数据源</small>
            <strong>{{ registeredCount }}</strong>
          </div>
        </div>
        <p>来自平台库的真实登记记录</p>
      </article>
      <article class="ds-metric">
        <div class="ds-metric-main">
          <span class="ds-metric-icon ds-metric-icon-green">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M9 3H5a2 2 0 0 0-2 2v4m6-6h10a2 2 0 0 1 2 2v4M9 3v18m0 0h10a2 2 0 0 0 2-2V9M9 21H5a2 2 0 0 1-2-2V9m0 0h18" />
            </svg>
          </span>
          <div>
            <small>可用数据源数量</small>
            <strong>{{ availableCount }}</strong>
          </div>
        </div>
        <p>当前处于激活状态的数据源</p>
      </article>
      <article class="ds-metric">
        <div class="ds-metric-main">
          <span class="ds-metric-icon ds-metric-icon-slate">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M22 12H2" />
              <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
              <path d="M6 16h.01M10 16h.01" />
            </svg>
          </span>
          <div>
            <small>已禁用数据源数量</small>
            <strong>{{ disabledCount }}</strong>
          </div>
        </div>
        <p>已禁用，不能创建新的采集作业与画像</p>
      </article>
    </section>

    <p v-if="route.query.notice === 'not_found'" class="state-block error-state" role="alert">数据源不存在</p>

    <section class="ds-catalog">
      <div class="ds-catalog-heading">
        <div>
          <span class="ds-kicker ds-kicker-sm">DATA SOURCE CATALOG</span>
          <h2>数据源连接</h2>
        </div>
        <button type="button" class="ds-refresh" :disabled="loading" @click="reload">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" />
            <path d="M21 3v5h-5" />
            <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" />
            <path d="M8 16H3v5" />
          </svg>
          刷新列表
        </button>
      </div>

      <!-- 筛选：数据库类型、状态、关键字（写入 query，刷新/返回可恢复） -->
      <form v-if="sources.length > 0" class="ds-filter-bar" @submit.prevent="applyKeyword">
        <label class="field ds-filter-type">
          <span>数据库类型</span>
          <select :value="dbTypeFilter" @change="onDbTypeChange(($event.target as HTMLSelectElement).value)">
            <option value="">全部类型</option>
            <option v-for="type in dbTypes" :key="type" :value="type">{{ dbTypeLabel(type) }}</option>
          </select>
        </label>
        <label class="field ds-filter-status">
          <span>状态筛选</span>
          <select :value="statusFilter" @change="onStatusChange(($event.target as HTMLSelectElement).value)">
            <option value="all">全部状态</option>
            <option value="active">启用</option>
            <option value="disabled">禁用</option>
          </select>
        </label>
        <label class="field ds-filter-keyword">
          <span>关键字搜索</span>
          <input
            v-model="keywordDraft"
            type="search"
            placeholder="名称 / source_id / 库名 / 主机"
          />
        </label>
        <button type="submit" class="button button-outline">搜索</button>
        <button v-if="hasFilter" type="button" class="button button-outline" @click="clearFilter">
          清除筛选
        </button>
      </form>

      <div v-if="loading" class="state-block" role="status">正在读取已登记的数据源…</div>
      <div v-else-if="error" class="state-block error-state" role="alert">
        <p>{{ error }}</p>
        <button class="button button-outline" @click="reload">重试</button>
      </div>
      <EmptyState
        v-else-if="sources.length === 0"
        title="还没有登记数据源"
        description="创建一个 PostgreSQL 连接，测试通过后选择 Schema，即可完成登记。"
      >
        <template #action>
          <RouterLink to="/data-sources/new" class="button button-primary">创建数据源</RouterLink>
        </template>
      </EmptyState>
      <EmptyState
        v-else-if="filteredSources.length === 0"
        title="没有符合条件的数据源"
        description="换个关键字、状态或数据库类型试试。"
      >
        <template #action>
          <button type="button" class="button button-outline" @click="clearFilter">清除筛选</button>
        </template>
      </EmptyState>
      <div v-else class="ds-source-grid">
        <article v-for="source in filteredSources" :key="source.source_id" class="ds-source-card">
          <div class="ds-source-top">
            <span class="ds-source-icon">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <rect width="20" height="8" x="2" y="2" rx="2" />
                <rect width="20" height="8" x="2" y="14" rx="2" />
                <path d="M6 6h.01M6 18h.01" />
              </svg>
            </span>
            <h3>
              <RouterLink :to="`/data-assets/${source.source_id}/profile`">{{ source.display_name }}</RouterLink>
            </h3>
            <span class="ds-status" :class="{ off: !source.is_active }">{{ source.is_active ? '激活' : '已禁用' }}</span>
            <span class="ds-spacer"></span>
            <RouterLink class="ds-manage" :to="`/data-sources/${source.source_id}`">管理</RouterLink>
          </div>
          <div class="ds-source-info">
            <p class="ds-source-id">{{ source.source_id }}</p>
            <p>{{ source.db_type }} · {{ source.db_version || '版本未知' }}</p>
            <p>{{ source.host }}:{{ source.port }}</p>
            <p>{{ source.db_name }}</p>
          </div>
          <p v-if="source.description" class="ds-source-desc">{{ source.description }}</p>
          <div class="ds-schemas">
            <span v-for="schema in source.selected_schemas" :key="schema">{{ schema }}</span>
          </div>
          <div class="ds-divider"></div>
          <div class="ds-source-foot">
            <div class="ds-source-meta">
              <p>登记时间 {{ displayDate(source.created_at) }}</p>
              <RouterLink :to="`/data-sources/${source.source_id}`">管理数据源连接 <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg></RouterLink>
            </div>
            <div class="ds-source-meta">
              <p v-if="source.last_collected_at">{{ displayDate(source.last_collected_at) }}</p>
              <span v-else></span>
              <RouterLink
                :to="{
                  path: '/workspaces',
                  query: { source_id: source.source_id, db_type: source.db_type },
                }"
              >相关工作区 <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg></RouterLink>
            </div>
          </div>
        </article>
      </div>
    </section>
  </div>
</template>

<style scoped>
.ds-filter-bar {
  display: flex;
  align-items: end;
  gap: 12px;
  flex-wrap: wrap;
  margin: 14px 0 4px;
}
.ds-filter-bar .field {
  margin: 0;
}
.ds-filter-status {
  width: 104px;
}
.ds-filter-type {
  width: 128px;
}
.ds-filter-keyword {
  width: 200px;
  max-width: 100%;
}
.ds-filter-bar .field input,
.ds-filter-bar .field select {
  width: 100%;
  height: 32px;
  padding: 0 10px;
  font-size: 12px;
  line-height: 32px;
}
.ds-filter-bar .field > span:first-child {
  display: block;
  color: #66788f;
  font-size: 11px;
  font-weight: 700;
  margin-bottom: 5px;
}
.ds-filter-bar .button {
  min-height: 32px;
  height: 32px;
  padding: 0 12px;
  font-size: 12px;
}
</style>
