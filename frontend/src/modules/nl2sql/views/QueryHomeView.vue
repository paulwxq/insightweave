<script setup lang="ts">
/**
 * 智能问数首页。筛选和页码写在 /query 上，打开会话和新建使用 push。
 * 历史分析尚未接入，右侧保留与原型一致的筛选框，列表留空。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { nl2sqlBaseUrl } from '../api/client'
import type { SessionInfo } from '../api/types'
import { formatNl2sqlTime, sessionKeyword } from '../format'
import { queryHomeTarget } from '../queryHomeLink'
import { createSessionList } from '../sessionList'
import { useSourceCatalog } from '../sourceCatalog'

const HOME_PAGE_SIZE = 6

const route = useRoute()
const router = useRouter()
const list = createSessionList(HOME_PAGE_SIZE)
const { sessions, loading, error, totalAll, totalFiltered, totalTurnsAll, loadPage } = list
const catalog = useSourceCatalog()
const {
  error: sourcesError,
  load: reloadSources,
  choices,
  databaseLabel,
  inactive,
} = catalog
const configured = ref(true)
const configMessage = ref('')
const draft = ref('')
const searchError = ref('')
const analysisSource = ref('')
const analysisDraft = ref('')
const analysisSearchError = ref('')

try {
  nl2sqlBaseUrl()
} catch (caught) {
  configured.value = false
  configMessage.value = caught instanceof Error ? caught.message : '查询服务未配置'
}

const sourceId = computed(() =>
  typeof route.query.source_id === 'string' ? route.query.source_id : '',
)
const keyword = computed(() => {
  const raw = typeof route.query.q === 'string' ? route.query.q : ''
  return raw.trim()
})
const page = computed(() => {
  const raw = typeof route.query.page === 'string' ? route.query.page : ''
  if (!/^[1-9]\d*$/.test(raw)) return 1
  const value = Number(raw)
  return value <= 10000 ? value : 1
})

const countsKnown = computed(() => totalAll.value !== null && totalFiltered.value !== null)
const pageCount = computed(() => {
  if (totalFiltered.value === null) return 0
  return Math.max(1, Math.ceil(totalFiltered.value / HOME_PAGE_SIZE))
})
const pageNumbers = computed(() => {
  const count = pageCount.value
  const current = page.value
  if (count <= 7) return Array.from({ length: count }, (_, index) => index + 1)
  const start = Math.max(1, Math.min(current - 2, count - 4))
  const end = Math.min(count, start + 4)
  return Array.from({ length: end - start + 1 }, (_, index) => start + index)
})
const emptyText = computed(() => {
  if (!countsKnown.value) return ''
  if (totalAll.value === 0) return '暂无对话'
  if (totalFiltered.value === 0) return '没有符合条件的对话'
  return '暂无对话'
})

function queryOf(nextSource: string, nextKeyword: string, nextPage = 1): Record<string, string> {
  const query: Record<string, string> = {}
  if (nextSource) query.source_id = nextSource
  if (nextKeyword) query.q = nextKeyword
  if (nextPage > 1) query.page = String(nextPage)
  return query
}

function replaceFilters(nextSource: string, nextKeyword: string, nextPage = 1) {
  void router.replace({ path: '/query', query: queryOf(nextSource, nextKeyword, nextPage) })
}

function onSourceChange(event: Event) {
  replaceFilters((event.target as HTMLSelectElement).value, keyword.value)
}

function submitSearch() {
  const result = sessionKeyword(draft.value)
  searchError.value = result.error
  if (result.error) return
  replaceFilters(sourceId.value, result.keyword)
}

function resetFilters() {
  searchError.value = ''
  draft.value = ''
  void router.replace({ path: '/query' })
}

function onAnalysisSourceChange(event: Event) {
  analysisSource.value = (event.target as HTMLSelectElement).value
}

function submitAnalysisSearch() {
  const result = sessionKeyword(analysisDraft.value)
  analysisSearchError.value = result.error
  if (result.error) return
  analysisDraft.value = result.keyword
}

function resetAnalysisFilters() {
  analysisSearchError.value = ''
  analysisDraft.value = ''
  analysisSource.value = ''
}

function goToPage(nextPage: number) {
  if (loading.value || nextPage < 1 || nextPage > pageCount.value || nextPage === page.value) return
  replaceFilters(sourceId.value, keyword.value, nextPage)
}

const newQueryTo = computed(() => ({
  path: '/query/new',
  query: {
    home: queryHomeTarget({
      source_id: sourceId.value,
      q: keyword.value,
      page: String(page.value),
    }),
  },
}))

function openSession(sessionId: string) {
  void router.push({
    name: 'nl2sql-query-session',
    params: { sessionId },
    query: {
      home: queryHomeTarget({
        source_id: sourceId.value,
        q: keyword.value,
        page: String(page.value),
      }),
    },
  })
}

function stateText(session: SessionInfo): string {
  if (session.state === 'invalidated') return '已失效'
  if (!session.continuable) return '待确认'
  return ''
}

async function settlePage() {
  if (!configured.value) return
  const requested = page.value
  const actual = await loadPage(
    { sourceId: sourceId.value || undefined, q: keyword.value || undefined },
    requested,
  )
  if (actual === null) return
  const canonical = actual > 1 ? String(actual) : undefined
  const current = typeof route.query.page === 'string' ? route.query.page : undefined
  if (current !== canonical) replaceFilters(sourceId.value, keyword.value, actual)
}

onMounted(() => {
  if (configured.value) void catalog.load()
})

watch(keyword, (value) => {
  draft.value = value
  searchError.value = ''
}, { immediate: true })

watch(
  () => `${sourceId.value}\n${keyword.value}\n${page.value}`,
  () => {
    void settlePage()
  },
  { immediate: true },
)
</script>

<template>
  <div class="ds-page query-home">
    <header class="ds-header">
      <div class="ds-title">
        <span class="ds-kicker">NL2SQL</span>
        <h1>智能问数</h1>
        <p>查看历史对话，或选择数据库开启一次新的查询。</p>
      </div>
    </header>

    <div v-if="!configured" class="state-block error-state">
      <h3>查询服务未配置</h3>
      <p>{{ configMessage }}</p>
    </div>

    <template v-else>
      <section class="ds-metrics home-metrics" aria-label="问数统计">
        <article class="stat-card stat-query">
          <div class="stat-head">
            <div class="stat-title">
              <span class="stat-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24"><path d="M14 9a2 2 0 0 1-2 2H6l-4 4V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2z" /><path d="M18 9h2a2 2 0 0 1 2 2v11l-4-4h-6a2 2 0 0 1-2-2v-1" /></svg>
              </span>
              查询会话
            </div>
            <span class="stat-pill"><i></i>历史累计</span>
          </div>
          <div class="stat-values">
            <p><span>会话次数</span><strong>{{ totalAll === null ? '—' : totalAll }}</strong></p>
            <p><span>总问答次数</span><strong>{{ totalTurnsAll === null ? '—' : totalTurnsAll }}</strong></p>
          </div>
          <small>已启动的会话与累计问答次数</small>
        </article>
        <article class="stat-card stat-analysis">
          <div class="stat-head">
            <div class="stat-title">
              <span class="stat-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24"><path d="M3 6h18" /><path d="M7 12h10" /><path d="M10 18h4" /></svg>
              </span>
              分析统计
            </div>
            <span class="stat-pill"><i></i>历史累计</span>
          </div>
          <div class="stat-values">
            <p><span>分析次数</span><strong>—</strong></p>
            <p><span>总问答次数</span><strong>—</strong></p>
          </div>
          <small>全部分析与累计问答次数</small>
        </article>
      </section>

      <div class="home-columns">
        <section class="home-column" aria-label="历史对话">
          <div class="column-head">
            <h2>历史对话</h2>
            <div class="column-actions">
              <span class="count-pill">共 {{ totalFiltered === null ? '—' : totalFiltered }} 条对话</span>
              <RouterLink :to="newQueryTo" class="home-create">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <polyline points="4 17 10 11 4 5" />
                  <line x1="12" x2="20" y1="19" y2="19" />
                </svg>
                新建查询
              </RouterLink>
            </div>
          </div>

          <form class="home-filters" @submit.prevent="submitSearch">
            <label>
              数据库
              <select :value="sourceId" @change="onSourceChange">
                <option value="">全部数据库</option>
                <option v-for="item in choices(sourceId)" :key="item.value" :value="item.value">
                  {{ item.label }}
                </option>
              </select>
            </label>
            <label>
              标题
              <span class="search-line">
                <span class="search-field">
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <circle cx="11" cy="11" r="7" />
                    <path d="m20 20-3.5-3.5" />
                  </svg>
                  <input v-model="draft" type="search" placeholder="搜索对话标题" />
                </span>
                <button type="submit" class="home-search">搜索</button>
                <button type="button" class="home-reset" @click="resetFilters">重置</button>
              </span>
            </label>
          </form>
          <p v-if="searchError" class="filter-error">{{ searchError }}</p>
          <p v-if="sourcesError" class="filter-error">
            {{ sourcesError }}
            <button type="button" class="home-reset" @click="reloadSources()">重试</button>
          </p>

          <div class="column-rule"></div>

          <div v-if="loading && sessions.length === 0" class="column-state">正在加载对话…</div>
          <div v-else-if="error && sessions.length === 0" class="column-state column-error">
            <p>{{ error }}</p>
            <button type="button" class="home-reset" @click="settlePage">重试</button>
          </div>
          <div v-else-if="sessions.length === 0" class="column-state">{{ emptyText }}</div>
          <ul v-else class="conversation-list">
            <li v-for="session in sessions" :key="session.session_id">
              <article class="conversation-row">
                <div class="conversation-copy">
                  <strong>{{ session.title }}</strong>
                  <span>
                    {{ databaseLabel(session.source_id) }}
                    <em v-if="inactive(session.source_id)">已停用</em>
                    <em v-if="stateText(session)" class="state">{{ stateText(session) }}</em>
                  </span>
                </div>
                <div class="conversation-side">
                  <time>{{ formatNl2sqlTime(session.last_active_at, true) }}</time>
                  <button type="button" class="continue" @click="openSession(session.session_id)">
                    继续对话
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M5 12h14" />
                      <path d="m13 6 6 6-6 6" />
                    </svg>
                  </button>
                </div>
              </article>
            </li>
          </ul>

          <div v-if="countsKnown && totalFiltered !== 0" class="pager">
            <p>第 {{ page }} / {{ pageCount }} 页 · 共 {{ totalFiltered }} 条</p>
            <div class="pager-pages">
              <button type="button" class="page-step" :disabled="page <= 1 || loading" aria-label="上一页" @click="goToPage(page - 1)">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6" /></svg>
              </button>
              <button
                v-for="number in pageNumbers"
                :key="number"
                type="button"
                class="page-number"
                :class="{ current: number === page }"
                :aria-current="number === page ? 'page' : undefined"
                :disabled="loading"
                @click="goToPage(number)"
              >
                {{ number }}
              </button>
              <button type="button" class="page-step" :disabled="page >= pageCount || loading" aria-label="下一页" @click="goToPage(page + 1)">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
              </button>
            </div>
          </div>
        </section>

        <section class="home-column" aria-label="历史分析">
          <div class="column-head">
            <h2>历史分析</h2>
            <div class="column-actions">
              <span class="count-pill">共 — 份分析</span>
              <button type="button" class="home-create" title="即将开放">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M12 5v14" />
                  <path d="M5 12h14" />
                </svg>
                新建分析
              </button>
            </div>
          </div>

          <form class="home-filters" @submit.prevent="submitAnalysisSearch">
            <label>
              数据库
              <select :value="analysisSource" @change="onAnalysisSourceChange">
                <option value="">全部数据库</option>
                <option v-for="item in choices(analysisSource)" :key="item.value" :value="item.value">
                  {{ item.label }}
                </option>
              </select>
            </label>
            <label>
              标题
              <span class="search-line">
                <span class="search-field">
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <circle cx="11" cy="11" r="7" />
                    <path d="m20 20-3.5-3.5" />
                  </svg>
                  <input v-model="analysisDraft" type="search" placeholder="搜索分析标题" />
                </span>
                <button type="submit" class="home-search">搜索</button>
                <button type="button" class="home-reset" @click="resetAnalysisFilters">重置</button>
              </span>
            </label>
          </form>
          <p v-if="analysisSearchError" class="filter-error">{{ analysisSearchError }}</p>
          <div class="column-rule"></div>
        </section>
      </div>
    </template>
  </div>
</template>

<style scoped>
.home-metrics {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.stat-card {
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 22px 24px;
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 18px;
  box-shadow: 0 6px 20px rgb(23 41 63 / 5%);
}

.stat-card::before {
  content: "";
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 3px;
}

.stat-query::before {
  background: linear-gradient(270deg, #2e7cd6 0%, rgb(46 124 214 / 0%) 100%);
}

.stat-analysis::before {
  background: linear-gradient(270deg, #7a6fd0 0%, rgb(122 111 208 / 0%) 100%);
}

.stat-head,
.stat-title,
.stat-pill {
  display: flex;
  align-items: center;
}

.stat-head {
  justify-content: space-between;
  gap: 12px;
}

.stat-title {
  gap: 10px;
  color: #17293f;
  font-size: 14px;
  font-weight: 700;
}

.stat-icon {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  flex-shrink: 0;
}

.stat-icon svg {
  width: 17px;
  height: 17px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.stat-query .stat-icon {
  background: #e3eefb;
  border: 1px solid rgb(46 124 214 / 14%);
  color: #2e7cd6;
}

.stat-analysis .stat-icon {
  background: #efedfa;
  border: 1px solid rgb(122 111 208 / 14%);
  color: #7a6fd0;
}

.stat-pill {
  gap: 6px;
  padding: 5px 9px;
  border-radius: 20px;
  font-size: 10px;
  white-space: nowrap;
}

.stat-pill i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
}

.stat-query .stat-pill {
  background: #e3eefb;
  color: #2e7cd6;
}

.stat-query .stat-pill i {
  background: #2e7cd6;
}

.stat-analysis .stat-pill {
  background: #efedfa;
  color: #7a6fd0;
}

.stat-analysis .stat-pill i {
  background: #7a6fd0;
}

.stat-values {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 12px 16px;
  border-radius: 12px;
}

.stat-query .stat-values {
  background: #f5f9fe;
}

.stat-analysis .stat-values {
  background: #f8f7fd;
}

.stat-values p {
  flex: 1;
  min-width: 0;
  margin: 0;
}

.stat-values p + p {
  border-left: 1px solid #d9e3ef;
  padding-left: 20px;
}

.stat-values span {
  display: block;
  color: #66788f;
  font-size: 11px;
  letter-spacing: 0.4px;
}

.stat-values strong {
  display: block;
  margin-top: 4px;
  color: #17293f;
  font-family: "Noto Serif SC", "Songti SC", Georgia, serif;
  font-size: 30px;
  font-weight: 700;
  line-height: 35px;
}

.stat-card > small {
  color: #66788f;
  font-size: 11px;
}

.home-columns {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
  align-items: start;
}

.home-column {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 28px;
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 20px;
}

.column-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.column-head h2 {
  margin: 0;
  font-family: "Noto Serif SC", "Songti SC", Georgia, serif;
  font-size: 18px;
  font-weight: 700;
  color: #17293f;
}

.column-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.count-pill {
  padding: 6px 12px;
  border: 1px solid #d9e3ef;
  border-radius: 20px;
  background: #eef3f9;
  color: #66788f;
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}

.home-create,
.home-search,
.home-reset,
.page-step,
.page-number,
.continue {
  font: inherit;
}

.home-create {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 12px;
  border: 0;
  border-radius: 12px;
  background: #2e7cd6;
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  line-height: normal;
  white-space: nowrap;
}

.home-create:hover {
  background: #2468c0;
}

.home-create svg,
.continue svg,
.page-step svg,
.search-field svg {
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.home-create svg {
  width: 12px;
  height: 12px;
}

.home-filters {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.home-filters label {
  display: grid;
  gap: 8px;
  color: #66788f;
  font-size: 12px;
}

.home-filters select,
.search-field {
  width: 100%;
  border: 1px solid #d9e3ef;
  border-radius: 12px;
  background: #fff;
  color: #17293f;
}

.home-filters select {
  height: 42px;
  padding: 0 14px;
}

.search-line {
  display: flex;
  align-items: center;
  gap: 10px;
}

.search-field {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
  padding: 0 14px;
}

.search-field svg {
  width: 14px;
  height: 14px;
  color: #66788f;
  flex-shrink: 0;
}

.search-field input {
  width: 100%;
  min-width: 0;
  height: 40px;
  border: 0;
  background: transparent;
  color: #17293f;
  font-size: 13px;
  outline: none;
}

.home-search,
.home-reset {
  flex-shrink: 0;
  height: 30px;
  padding: 0 10px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 700;
}

.home-search {
  border: 0;
  background: #2e7cd6;
  color: #fff;
}

.home-search:hover {
  background: #2468c0;
}

.home-reset {
  border: 1px solid #d9e3ef;
  background: #fff;
  color: #17293f;
}

.home-reset:hover {
  background: #eef3f9;
}

.filter-error {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: -6px 0 0;
  color: #9a6b32;
  font-size: 13px;
}

.column-rule {
  height: 1px;
  background: #d9e3ef;
}

.column-state {
  padding: 28px 8px;
  color: #66788f;
  text-align: center;
  font-size: 13px;
}

.column-error {
  color: #ac4d58;
}

.column-error p {
  margin: 0 0 12px;
}

.conversation-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 12px;
}

.conversation-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 18px;
  border: 1px solid #d9e3ef;
  border-radius: 14px;
  background: #fff;
}

.conversation-copy {
  min-width: 0;
  flex: 1;
}

.conversation-copy strong,
.conversation-copy span {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conversation-copy strong {
  color: #17293f;
  font-size: 14px;
}

.conversation-copy span,
.conversation-side time {
  color: #66788f;
  font-size: 12px;
}

.conversation-copy span {
  margin-top: 4px;
}

.conversation-copy em {
  margin-left: 8px;
  font-style: normal;
  color: #9a6b32;
}

.conversation-copy em.state {
  color: #8a5a44;
}

.conversation-side {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-shrink: 0;
}

.conversation-side time {
  font-size: 11px;
  white-space: nowrap;
}

.continue {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0;
  border: 0;
  background: transparent;
  color: #2e7cd6;
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}

.continue svg {
  width: 12px;
  height: 12px;
}

.continue:hover {
  color: #2468c0;
}

.pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.pager p {
  margin: 0;
  color: #66788f;
  font-size: 12px;
  white-space: nowrap;
}

.pager-pages {
  display: flex;
  align-items: center;
  gap: 6px;
}

.page-step,
.page-number {
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid #d9e3ef;
  border-radius: 8px;
  background: #fff;
  color: #17293f;
  font-size: 12px;
}

.page-number.current {
  border-color: #2e7cd6;
  background: #2e7cd6;
  color: #fff;
  font-weight: 700;
}

.page-step svg {
  width: 14px;
  height: 14px;
  color: #66788f;
}

.page-step:disabled,
.page-number:disabled {
  opacity: 0.45;
}

@media (max-width: 1100px) {
  .home-columns,
  .home-metrics {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 720px) {
  .column-head,
  .conversation-row,
  .pager,
  .search-line {
    flex-direction: column;
    align-items: stretch;
  }

  .column-actions,
  .conversation-side,
  .pager-pages {
    justify-content: space-between;
  }

  .home-search,
  .home-reset {
    width: 100%;
  }
}
</style>
