<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { displayDate } from '../api/format'
import type { DataSourceListItem } from '../api/types'
import type { HomeError, HomeJob } from '../composables/useSourceHome'

const props = defineProps<{
  kicker: string
  title: string
  description: string
  cardTitle: string
  cardKicker: string
  cardDescription: string
  listTitle: string
  metrics: { label: string; hint: string }[]
  metricValues: number[]
  newLabel: string
  rerunLabel: string
  newTo: string
  newEnabled: boolean
  rerunTo: (item: HomeJob) => string
  rerunEnabled: (item: HomeJob) => boolean
  rerunTitle: (item: HomeJob) => string
  sources: DataSourceListItem[]
  filteredSources: DataSourceListItem[]
  dbType: string
  selectedSourceId: string
  pageItems: HomeJob[]
  itemKey: (item: HomeJob) => string
  itemTitle: (item: HomeJob) => string
  selectedKey: string
  sortDesc: boolean
  page: number
  pageCount: number
  pageNumbers: number[]
  total: number
  refreshing: boolean
  homeLoading: boolean
  homeError: HomeError | null
  hasSnapshot: boolean
  sourcesError: string
  sourcesLoading: boolean
}>()

const emit = defineEmits<{
  'update:dbType': [value: string]
  chooseSource: [sourceId: string]
  selectItem: [key: string]
  refresh: []
  retry: []
  reloadSources: []
  toggleSort: []
  goToPage: [page: number]
}>()

const router = useRouter()

const showHomeErrorOnly = computed(() => Boolean(props.homeError) && !props.hasSnapshot)

function goBack() {
  const previous = window.history.state?.back
  if (typeof previous === 'string' && previous.length > 0) {
    router.back()
    return
  }
  router.push('/data-sources')
}

function statusText(status: string): string {
  if (status === 'running') return '运行中'
  if (status === 'failed') return '失败'
  return '成功'
}

function stepLine(steps: string[]): string {
  return steps.length > 0 ? `步骤 ${steps.join(' / ')}` : ''
}

const dbTypeOptions = ['PostgreSQL', 'MySQL', 'Excel', 'CSV']
const typeMenuOpen = ref(false)
const typeChoices = [{ value: '', label: '全部' }, ...dbTypeOptions.map((type) => ({ value: type, label: type }))]

function chooseType(value: string) {
  emit('update:dbType', value)
  typeMenuOpen.value = false
}

function subtitle(source: DataSourceListItem): string {
  return `${source.db_type} · ${source.host}:${source.port} · ${source.db_name}`
}
</script>

<template>
  <div class="ds-jobs home-work">
    <div v-if="sourcesLoading" class="state-block" role="status">正在读取已登记的数据源…</div>
    <div v-else-if="sourcesError" class="state-block error-state" role="alert">
      <p>{{ sourcesError }}</p>
      <button type="button" class="button button-outline" @click="emit('reloadSources')">重试</button>
    </div>

    <template v-else>
      <header class="ds-jobs-header">
        <div>
          <span class="ds-kicker">{{ kicker }}</span>
          <h1>{{ title }}</h1>
          <p>{{ description }}</p>
        </div>
        <button type="button" class="ds-jobs-back" @click="goBack">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
          返回
        </button>
      </header>

      <section class="ds-metrics" aria-label="统计">
        <article v-for="(metric, index) in metrics" :key="metric.label" class="ds-metric">
          <small>{{ metric.label }}</small>
          <strong>{{ hasSnapshot ? metricValues[index] ?? 0 : '—' }}</strong>
          <p>{{ metric.hint }}</p>
        </article>
      </section>

      <section class="ds-jobs-card">
        <div class="home-card-head">
          <span class="ds-kicker">{{ cardKicker }}</span>
          <h2>{{ cardTitle }}</h2>
          <p>{{ cardDescription }}</p>
        </div>
        <div class="home-work-body">
          <aside class="home-work-sources">
            <div class="home-col-head">
              <span>数据源列表</span>
              <div class="home-type-filter" :class="{ open: typeMenuOpen }">
                <button
                  type="button"
                  class="home-type-trigger"
                  aria-haspopup="listbox"
                  aria-label="数据库类型"
                  :aria-expanded="typeMenuOpen"
                  @click="typeMenuOpen = !typeMenuOpen"
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18" /><path d="M6 12h12" /><path d="M10 18h4" /></svg>
                  <span>{{ dbType || '全部' }}</span>
                  <svg class="home-type-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
                </button>
                <button v-if="typeMenuOpen" type="button" class="home-type-backdrop" aria-label="关闭数据库类型菜单" @click="typeMenuOpen = false"></button>
                <ul v-if="typeMenuOpen" class="home-type-menu" role="listbox">
                  <li v-for="choice in typeChoices" :key="choice.label">
                    <button
                      type="button"
                      role="option"
                      :aria-selected="dbType === choice.value"
                      :class="{ current: dbType === choice.value }"
                      @click="chooseType(choice.value)"
                    >
                      {{ choice.label }}
                    </button>
                  </li>
                </ul>
              </div>
            </div>
            <p v-if="sources.length === 0" class="ds-jobs-empty">还没有登记的数据源</p>
            <p v-else-if="filteredSources.length === 0" class="ds-jobs-empty">没有符合筛选的数据源</p>
            <button
              v-for="source in filteredSources"
              :key="source.source_id"
              type="button"
              class="home-work-source"
              :class="{ current: source.source_id === selectedSourceId }"
              @click="emit('chooseSource', source.source_id)"
            >
              <span class="home-source-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5" /><path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3" /></svg>
              </span>
              <span class="home-source-text">
                <strong>{{ source.display_name }}</strong>
                <span>{{ subtitle(source) }}</span>
                <em v-if="!source.is_active">已禁用</em>
              </span>
              <svg v-if="source.source_id === selectedSourceId" class="home-mark" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="m8 12 2.5 2.5L16 9" /></svg>
            </button>
          </aside>

          <div class="home-work-jobs">
            <div class="home-col-head">
              <span>{{ listTitle }}</span>
              <div class="home-work-tools">
                <button type="button" class="ds-refresh" :disabled="refreshing || homeLoading || !selectedSourceId" @click="emit('refresh')">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12a9 9 0 0 0-15.5-6.3L3 8" /><path d="M3 3v5h5" /><path d="M3 12a9 9 0 0 0 15.5 6.3L21 16" /><path d="M21 21v-5h-5" /></svg>
                  刷新
                </button>
                <RouterLink v-if="newEnabled" class="home-work-new" :to="newTo">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14" /><path d="M5 12h14" /></svg>
                  {{ newLabel }}
                </RouterLink>
                <span v-else class="home-work-new off" aria-disabled="true">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14" /><path d="M5 12h14" /></svg>
                  {{ newLabel }}
                </span>
                <button type="button" class="home-work-sort" @click="emit('toggleSort')">
                  <svg viewBox="0 0 24 24" aria-hidden="true" :class="{ asc: !sortDesc }"><path d="M12 5v14" /><path d="m19 12-7 7-7-7" /></svg>
                  执行时间{{ sortDesc ? '倒序' : '正序' }}
                </button>
              </div>
            </div>

            <p v-if="homeLoading" class="ds-jobs-state">正在读取作业…</p>
            <div v-else-if="showHomeErrorOnly" class="state-block error-state" role="alert">
              <p>{{ homeError?.message }}</p>
              <p v-if="homeError?.code === 'not_found'">左侧数据源列表已过期。</p>
              <button
                v-if="homeError?.code === 'not_found'"
                type="button"
                class="button button-outline"
                @click="emit('reloadSources')"
              >
                刷新数据源列表
              </button>
              <button v-else type="button" class="button button-outline" @click="emit('retry')">重试</button>
            </div>
            <template v-else>
              <div v-if="homeError" class="state-block error-state" role="alert">
                <p>{{ homeError.message }}</p>
                <p v-if="homeError.code === 'not_found'">左侧数据源列表已过期。</p>
                <button
                  v-if="homeError.code === 'not_found'"
                  type="button"
                  class="button button-outline"
                  @click="emit('reloadSources')"
                >
                  刷新数据源列表
                </button>
                <button v-else type="button" class="button button-outline" @click="emit('retry')">重试</button>
              </div>

              <div class="home-work-list">
                <p v-if="!selectedSourceId" class="ds-jobs-empty">没有选中的数据源</p>
                <p v-else-if="pageItems.length === 0" class="ds-jobs-empty">该数据源还没有作业</p>
                <div
                  v-for="item in pageItems"
                  :key="itemKey(item)"
                  class="home-work-row"
                  :class="{ current: itemKey(item) === selectedKey }"
                >
                  <button type="button" class="home-job-pick" @click="emit('selectItem', itemKey(item))">
                    <span class="home-job-main">
                      <span class="home-job-icon" :class="item.status" aria-hidden="true">
                        <svg v-if="item.status === 'failed'" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="m15 9-6 6" /><path d="m9 9 6 6" /></svg>
                        <svg v-else-if="item.status === 'running'" viewBox="0 0 24 24"><path d="M12 2v4" /><path d="m16.2 7.8 2.9-2.9" /><path d="M18 12h4" /><path d="m16.2 16.2 2.9 2.9" /><path d="M12 18v4" /><path d="m4.9 19.1 2.9-2.9" /><path d="M2 12h4" /><path d="m4.9 4.9 2.9 2.9" /></svg>
                        <svg v-else viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="m8 12 2.5 2.5L16 9" /></svg>
                      </span>
                      <span class="home-job-text">
                        <strong>{{ itemTitle(item) }}</strong>
                        <span class="home-job-time">执行时间 {{ displayDate(item.executed_at) }}</span>
                      </span>
                    </span>
                    <span class="home-job-steps">{{ stepLine(item.steps) }}</span>
                    <span class="home-status" :class="item.status">{{ statusText(item.status) }}</span>
                  </button>
                  <RouterLink
                    v-if="rerunEnabled(item)"
                    class="home-row-rerun"
                    :to="rerunTo(item)"
                    :title="rerunTitle(item) || undefined"
                  >{{ rerunLabel }}</RouterLink>
                  <span
                    v-else
                    class="home-row-rerun off"
                    aria-disabled="true"
                    :title="rerunTitle(item) || undefined"
                  >{{ rerunLabel }}</span>
                </div>
              </div>

              <div class="ds-jobs-pager">
                <span>共 {{ total }} 条记录 · 第 {{ page }} / {{ pageCount }} 页 · 每页 10 条</span>
                <div>
                  <button type="button" :disabled="page <= 1" aria-label="上一页" @click="emit('goToPage', page - 1)">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6" /></svg>
                  </button>
                  <button
                    v-for="number in pageNumbers"
                    :key="number"
                    type="button"
                    :class="{ current: number === page }"
                    @click="emit('goToPage', number)"
                  >
                    {{ number }}
                  </button>
                  <button type="button" :disabled="page >= pageCount" aria-label="下一页" @click="emit('goToPage', page + 1)">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
                  </button>
                </div>
              </div>
            </template>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>
