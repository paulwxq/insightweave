<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { listAssets } from '../api/assets'
import { displayDate } from '../api/format'
import type { AssetCatalogItem, AssetCatalogResponse } from '../api/types'
import { formatSize } from '../utils/format'

const PAGE_SIZE = 4
const TYPE_OPTIONS = ['全部类型', 'PostgreSQL', 'MySQL', 'SQLite', 'CSV', 'Excel']

const catalog = ref<AssetCatalogResponse | null>(null)
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')
const keyword = ref('')
const typeFilter = ref('全部类型')
const sortDesc = ref(true)
const page = ref(1)

const items = computed(() => catalog.value?.items ?? [])
const filtered = computed(() => {
  const text = keyword.value.trim().toLowerCase()
  const rows = items.value.filter((item) => {
    const typeMatches = typeFilter.value === '全部类型' || item.db_type_label === typeFilter.value
    const nameMatches = !text || item.display_name.toLowerCase().includes(text)
    return typeMatches && nameMatches
  })
  return [...rows].sort((left, right) => {
    const delta = left.collected_at.localeCompare(right.collected_at)
    return sortDesc.value ? -delta : delta
  })
})
const pageCount = computed(() => Math.max(1, Math.ceil(filtered.value.length / PAGE_SIZE)))
const safePage = computed(() => Math.min(page.value, pageCount.value))
const visible = computed(() => {
  const start = (safePage.value - 1) * PAGE_SIZE
  return filtered.value.slice(start, start + PAGE_SIZE)
})
const pageNumbers = computed(() => Array.from({ length: pageCount.value }, (_, index) => index + 1))

watch([keyword, typeFilter, sortDesc], () => {
  page.value = 1
})

function markClass(item: AssetCatalogItem): string {
  return item.db_type.toLowerCase()
}

async function reload() {
  if (catalog.value) refreshing.value = true
  else loading.value = true
  error.value = ''
  try {
    catalog.value = await listAssets()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '数据资产加载失败。'
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

onMounted(reload)
</script>

<template>
  <div class="assets-page">
    <header class="page-header">
      <div>
        <div class="eyebrow">DATA ASSETS</div>
        <h1>数据资产</h1>
        <p>把常用数据库统一登记、画像并复用。资产属于系统，只有明确绑定后才会进入某个分析项目。</p>
      </div>
      <div class="detail-actions">
        <RouterLink to="/data-sources/new" class="button button-outline">创建数据源连接</RouterLink>
        <button type="button" class="button button-primary" disabled title="导入尚未开放">导入数据资产</button>
      </div>
    </header>

    <section class="overview-grid">
      <div class="metric-card">
        <span class="metric-icon">
          <svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v14a9 3 0 0 0 18 0V5" /><path d="M3 12a9 3 0 0 0 18 0" /></svg>
        </span>
        <small>可用数据源</small>
        <strong>{{ catalog ? catalog.source_count : '—' }}</strong>
        <p>PostgreSQL / MySQL / SQLite / CSV / Excel</p>
      </div>
      <div class="metric-card asset-metric-drive">
        <span class="metric-icon">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m22 12-4-8H6L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2Z" /><path d="M6 12h.01" /><path d="M10 12h.01" /></svg>
        </span>
        <small>可用表或视图的数量</small>
        <strong>{{ catalog ? catalog.object_count : '—' }}</strong>
        <p>Table/View/MV</p>
      </div>
      <div class="metric-card asset-metric-folder">
        <span class="metric-icon">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2" /></svg>
        </span>
        <small>已创建对话或项目</small>
        <strong>{{ catalog ? catalog.project_count : '—' }}</strong>
        <p>对话 / 分析项目 · 暂无记录</p>
      </div>
    </section>

    <section class="catalog-card">
      <div class="catalog-heading">
        <div>
          <div class="eyebrow">ASSET CATALOG</div>
          <h2>资产目录</h2>
          <p>已登记数据库的名称与画像概览，可刷新画像或删除资产。</p>
        </div>
        <span class="asset-count">{{ filtered.length }} 个数据库</span>
      </div>

      <div v-if="catalog && catalog.inconsistent_count > 0" class="asset-warning" role="status">
        有 {{ catalog.inconsistent_count }} 个数据源的当前发布缺少画像内容，未列入目录。
      </div>

      <div class="asset-toolbar">
        <label class="asset-search">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="8" /><path d="m21 21-4.3-4.3" /></svg>
          <input v-model="keyword" type="search" placeholder="搜索数据库名称…" />
        </label>
        <button type="button" class="asset-sort" @click="sortDesc = !sortDesc">
          数据采集时间
          <svg viewBox="0 0 24 24" aria-hidden="true" :class="{ asc: !sortDesc }"><path d="M12 5v14" /><path d="m19 12-7 7-7-7" /></svg>
        </button>
        <label class="asset-type">
          <select v-model="typeFilter" aria-label="数据库类型">
            <option v-for="option in TYPE_OPTIONS" :key="option">{{ option }}</option>
          </select>
        </label>
      </div>

      <div v-if="loading" class="state-block">正在读取数据资产…</div>
      <div v-else-if="error" class="state-block error-state">
        <p>{{ error }}</p>
        <button type="button" class="button button-outline" @click="reload">重试</button>
      </div>
      <div v-else-if="items.length === 0" class="state-block empty-state">
        <h3>还没有已发布的数据库画像</h3>
        <p>发布成功后，这里会显示该次发布对应的数据库画像。</p>
      </div>
      <div v-else-if="filtered.length === 0" class="state-block empty-state">
        <h3>没有匹配的数据库</h3>
        <p>换一个名称或类型后再试。</p>
      </div>
      <template v-else>
        <div class="asset-list">
          <article v-for="item in visible" :key="item.publish_id" class="asset-row">
            <div class="asset-lead">
              <span class="asset-mark" :class="markClass(item)">
                <svg viewBox="0 0 24 24" aria-hidden="true"><rect width="20" height="8" x="2" y="2" rx="2" /><rect width="20" height="8" x="2" y="14" rx="2" /><path d="M6 6h.01" /><path d="M6 18h.01" /></svg>
              </span>
              <div class="asset-copy">
                <div class="asset-name">
                  <RouterLink class="asset-name-link" :to="`/data-assets/${item.source_id}/profile`">{{ item.display_name }}</RouterLink>
                  <span class="asset-chip">可用</span>
                </div>
              </div>
            </div>
            <div class="asset-stats">
              <div class="asset-stat">
                <small>数据库版本</small>
                <strong>{{ item.server_version ? `${item.db_type_label} ${item.server_version}` : item.db_type_label }}</strong>
              </div>
              <div class="asset-stat connection">
                <small>连接信息</small>
                <strong>{{ item.connection }}</strong>
              </div>
              <div class="asset-stat">
                <small>表和视图的数量</small>
                <strong>{{ item.table_count }}</strong>
              </div>
              <div class="asset-stat">
                <small>总大小</small>
                <strong>{{ formatSize(item.size_bytes) }}</strong>
              </div>
              <div class="asset-stat time">
                <small>数据采集时间</small>
                <strong>{{ displayDate(item.collected_at) }}</strong>
              </div>
            </div>
            <button type="button" class="asset-refresh" :disabled="refreshing" aria-label="刷新画像" @click="reload">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12a9 9 0 0 1 15.5-6.3L21 8" /><path d="M21 3v5h-5" /><path d="M21 12a9 9 0 0 1-15.5 6.3L3 16" /><path d="M3 21v-5h5" /></svg>
            </button>
          </article>
        </div>
        <div class="asset-pager">
          <button type="button" :disabled="safePage <= 1" @click="page = safePage - 1">上一页</button>
          <button
            v-for="number in pageNumbers"
            :key="number"
            type="button"
            :class="{ current: number === safePage }"
            @click="page = number"
          >
            {{ number }}
          </button>
          <button type="button" :disabled="safePage >= pageCount" @click="page = safePage + 1">下一页</button>
        </div>
      </template>
    </section>
  </div>
</template>
