<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { listDataSources } from '../api/dataSources'
import { displayDate } from '../api/format'
import type { DataSourceListItem } from '../api/types'

const route = useRoute()
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

function activityTime(value: string | null | undefined): string {
  return value ? displayDate(value) : '—'
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

      <div v-if="loading" class="state-block" role="status">正在读取已登记的数据源…</div>
      <div v-else-if="error" class="state-block error-state" role="alert">
        <p>{{ error }}</p>
        <button class="button button-outline" @click="reload">重试</button>
      </div>
      <div v-else-if="sources.length === 0" class="state-block empty-state">
        <span class="empty-icon">▤</span>
        <h3>还没有登记数据源</h3>
        <p>创建一个 PostgreSQL 连接，测试通过后选择 Schema，即可完成登记。</p>
        <RouterLink to="/data-sources/new" class="button button-primary">创建数据源</RouterLink>
      </div>
      <div v-else class="ds-source-grid">
        <article v-for="source in sources" :key="source.source_id" class="ds-source-card">
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
              <p>最近采集 {{ activityTime(source.last_collected_at) }}</p>
              <RouterLink :to="`/data-sources/${source.source_id}/collection-jobs`">相关数据采集作业 <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg></RouterLink>
            </div>
            <div class="ds-source-meta">
              <p>最近发布 {{ activityTime(source.last_published_at) }}</p>
              <RouterLink :to="`/data-sources/${source.source_id}/publish-jobs`">相关画像发布作业 <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg></RouterLink>
            </div>
          </div>
        </article>
      </div>
    </section>
  </div>
</template>
