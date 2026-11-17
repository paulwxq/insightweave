<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listObjectOptions, listSourceObjects } from '../api/assets'
import { getDataSource } from '../api/dataSources'
import { ApiError } from '../api/http'
import PageBreadcrumbs from '../components/PageBreadcrumbs.vue'
import type { DataSourceListItem, ObjectsResponse } from '../api/types'

const route = useRoute()
const router = useRouter()
const sourceId = computed(() =>
  typeof route.params.sourceId === 'string' ? route.params.sourceId : '',
)
const routeSchema = computed(() =>
  typeof route.query.schema_name === 'string' ? route.query.schema_name : '',
)

const PAGE_SIZE = 20

const draft = ref({ object_type: '', schema_name: '', keyword: '' })
const applied = ref({ object_type: '', schema_name: '', keyword: '' })
const payload = ref<ObjectsResponse | null>(null)
const source = ref<DataSourceListItem | null>(null)
// 总页数从独立的 total 派生，而非会被 load() 中途清空的 payload：
// 否则请求在途时 pageCount 瞬塌为 1，会误触钳制把页码改回 1，
// 与已发出请求的页码错位（>1 页时表格与标题对不上）
const total = ref(0)
const schemaOptions = ref<string[]>([])
const loading = ref(true)
const loadError = ref('')
const page = ref(1)

// 请求序号：切源/连续筛选/翻页时，过期响应不得覆盖最新状态，过期 404
// 也不得触发跳转（doc 19.2 §3.7）
let listRequestId = 0
let optionsRequestId = 0
// 用户在本页改筛选并同步地址栏时，不要把对象类型和名称清空重来
let ignoreRouteSchema = false

const hasFilter = computed(() =>
  Boolean(
    applied.value.object_type || applied.value.schema_name || applied.value.keyword,
  ),
)

const countText = computed(() => {
  if (!payload.value || payload.value.state !== 'ready') return ''
  if (payload.value.total === 0) return '未找到符合条件的对象'
  return hasFilter.value
    ? `符合条件的 ${payload.value.total} 个对象`
    : `共 ${payload.value.total} 个对象`
})

const versionText = computed(() => {
  const version = source.value?.db_version?.trim()
  return version ? `PostgreSQL ${version}` : '版本未知'
})
const connectionText = computed(() => {
  if (!source.value) return ''
  return `${source.value.host}:${source.value.port}/${source.value.db_name}`
})

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const pageNumbers = computed(() =>
  Array.from({ length: pageCount.value }, (_, index) => index + 1),
)

function typeLabel(objectType: string): string {
  if (objectType === 'table') return '表'
  if (objectType === 'view') return '视图'
  if (objectType === 'materialized_view') return '物化视图'
  return objectType
}

function primaryKeyLabel(state: string): string {
  // 显示口径按 §8.1.1 / §8.1 / §9.2"有 / 无 / 未生成"：unknown 为结构
  // 信息缺失（table_profile=null 或主键节点缺失），与"无"（明确无主键）
  // 严格区分；"未知"一词保留给表分类 unknown
  if (state === 'present') return '有'
  if (state === 'none') return '无'
  return '未生成'
}

function formatRows(value: number | null): string {
  if (value === null) return '—'
  return value.toLocaleString('zh-CN')
}

function objectDetailPath(schemaName: string, objectName: string): string {
  const query = new URLSearchParams({ schema_name: schemaName, object_name: objectName })
  return `/data-assets/${sourceId.value}/objects/detail?${query.toString()}`
}

function leaveMissing() {
  return router.replace({ path: '/data-assets', query: { notice: 'not_found' } })
}

async function loadOptions() {
  const requestId = ++optionsRequestId
  try {
    const options = await listObjectOptions(sourceId.value, {
      object_type: applied.value.object_type || undefined,
    })
    if (requestId !== optionsRequestId) return
    schemaOptions.value = withSelectedSchema(options.state === 'ready' ? options.schemas : [])
  } catch {
    if (requestId === optionsRequestId) schemaOptions.value = withSelectedSchema([])
  }
}

function withSelectedSchema(names: string[]): string[] {
  const selected = applied.value.schema_name
  if (!selected || names.includes(selected)) return names
  return [...names, selected].sort((left, right) => (left < right ? -1 : left > right ? 1 : 0))
}

async function load() {
  const requestId = ++listRequestId
  loading.value = true
  loadError.value = ''
  payload.value = null
  try {
    const response = await listSourceObjects(sourceId.value, {
      page: page.value,
      page_size: PAGE_SIZE,
      object_type: applied.value.object_type || undefined,
      schema_name: applied.value.schema_name || undefined,
      keyword: applied.value.keyword || undefined,
    })
    if (requestId !== listRequestId) return
    total.value = response.state === 'ready' ? response.total : 0
    if (page.value > pageCount.value) {
      // 并发重发等使总数缩小时：钳到末页并重取，保证页码与数据配对；
      // 静默钳回会留下"页码 1 / 数据第 N 页"的错位
      page.value = pageCount.value
      await load()
      return
    }
    payload.value = response
  } catch (error) {
    if (requestId !== listRequestId) return
    if (
      error instanceof ApiError &&
      (error.code === 'not_found' || error.code === 'invalid_request')
    ) {
      await leaveMissing()
      return
    }
    loadError.value = error instanceof ApiError ? error.message : '对象列表加载失败'
  } finally {
    if (requestId === listRequestId) loading.value = false
  }
}

function applyFilters() {
  applied.value = { ...draft.value }
  page.value = 1
  const nextSchema = applied.value.schema_name
  if (nextSchema !== routeSchema.value) {
    ignoreRouteSchema = true
    const query = { ...route.query }
    if (nextSchema) query.schema_name = nextSchema
    else delete query.schema_name
    void router.replace({ query }).catch(() => {
      ignoreRouteSchema = false
    })
  }
  void load()
  void loadOptions()
}

async function loadSource() {
  const currentId = sourceId.value
  try {
    const row = await getDataSource(currentId)
    if (currentId !== sourceId.value) return
    source.value = row
  } catch {
    if (currentId === sourceId.value) source.value = null
  }
}

function applyRouteSchema() {
  page.value = 1
  const filters = { object_type: '', schema_name: routeSchema.value, keyword: '' }
  applied.value = { ...filters }
  draft.value = { ...filters }
  schemaOptions.value = routeSchema.value ? [routeSchema.value] : []
  void load()
  void loadOptions()
}

watch(sourceId, () => {
  source.value = null
  void loadSource()
}, { immediate: true })

watch(
  () => [sourceId.value, routeSchema.value] as const,
  () => {
    if (ignoreRouteSchema) {
      ignoreRouteSchema = false
      return
    }
    applyRouteSchema()
  },
  { immediate: true },
)


function goBack() {
  // doc 9 §7.4：对象列表固定返回该数据源的画像页，不再走浏览器历史
  void router.push(`/data-assets/${sourceId.value}/profile`)
}
</script>

<template>
  <div class="ds-jobs">
    <PageBreadcrumbs
      :segments="[
        { label: '数据资产', to: '/data-assets' },
        {
          label: source?.display_name ?? '数据源',
          to: `/data-assets/${sourceId}/profile`,
        },
        { label: '对象列表' },
      ]"
    />
    <header class="ds-jobs-header">
      <div>
        <span class="ds-kicker">DATABASE OBJECTS</span>
        <h1>数据库对象列表</h1>
        <p>当前数据源已发布 JSON 画像中的表、视图与物化视图，点击对象名称查看画像详情。</p>
      </div>
      <button type="button" class="ds-jobs-back" @click="goBack">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
        返回
      </button>
    </header>

    <section v-if="source" class="obj-source-bar">
      <span class="obj-source-title">数据库信息</span>
      <span class="obj-source-item"><span>版本号</span><strong class="plain">{{ versionText }}</strong></span>
      <span class="obj-source-rule" aria-hidden="true"></span>
      <span class="obj-source-item"><span>连接信息</span><strong class="mono">{{ connectionText }}</strong></span>
      <span class="obj-source-rule" aria-hidden="true"></span>
      <span class="obj-source-item"><span>数据源ID</span><strong class="mono">{{ source.source_id }}</strong></span>
    </section>

    <p v-if="loading" class="ds-jobs-state">正在读取对象列表…</p>
    <p v-else-if="loadError" class="ds-jobs-state error">{{ loadError }}</p>

    <template v-else-if="payload">
      <section class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">OBJECTS</span>
            <h2>对象列表</h2>
            <p>按 Schema 与对象名称稳定排序；计数来自本次列表接口。</p>
          </div>
          <div class="ds-jobs-actions">
            <span v-if="countText" class="ds-jobs-count">{{ countText }}</span>
          </div>
        </div>

        <div v-if="payload.state === 'not_published'" class="ds-jobs-empty">
          尚未发布 JSON 画像
        </div>
        <div v-else-if="payload.state === 'inconsistent'" class="ds-jobs-empty">
          JSON 画像发布状态不一致，暂无法展示对象
        </div>

        <template v-else>
          <div class="obj-filter">
            <label>
              <span>对象类型</span>
              <select v-model="draft.object_type">
                <option value="">全部</option>
                <option value="table">表</option>
                <option value="view">视图</option>
                <option value="materialized_view">物化视图</option>
              </select>
            </label>
            <label>
              <span>SCHEMA</span>
              <select v-model="draft.schema_name">
                <option value="">全部</option>
                <option v-for="name in schemaOptions" :key="name" :value="name">
                  {{ name }}
                </option>
              </select>
            </label>
            <label>
              <span>对象名称</span>
              <input
                v-model="draft.keyword"
                type="text"
                placeholder="按对象名称筛选"
                @keyup.enter="applyFilters"
              />
            </label>
            <button type="button" class="obj-search" @click="applyFilters">
              <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
              搜索
            </button>
          </div>

          <div class="ds-jobs-table">
            <div class="ds-jobs-row ds-jobs-head obj-list-row" role="row">
              <span>对象类型</span>
              <span>SCHEMA</span>
              <span>对象名称</span>
              <span>中文名称</span>
              <span>字段数量</span>
              <span>记录数</span>
              <span>主键</span>
            </div>
            <RouterLink
              v-for="item in payload.items"
              :key="`${item.schema_name}.${item.object_name}`"
              class="ds-jobs-row ds-jobs-item obj-row obj-list-row"
              role="row"
              :to="objectDetailPath(item.schema_name, item.object_name)"
            >
              <span>{{ typeLabel(item.object_type) }}</span>
              <span>{{ item.schema_name }}</span>
              <span class="obj-name">{{ item.object_name }}</span>
              <span>{{ item.object_comment || '暂无注释' }}</span>
              <span>{{ item.column_count }}</span>
              <span>{{ formatRows(item.total_rows) }}</span>
              <span>{{ primaryKeyLabel(item.primary_key_state) }}</span>
            </RouterLink>
            <p v-if="payload.items.length === 0" class="ds-jobs-empty">
              未找到符合条件的对象
            </p>
          </div>

          <div v-if="pageCount > 1" class="ds-jobs-pager">
            <span>{{ countText }} · 第 {{ page }} / {{ pageCount }} 页</span>
            <div>
              <button type="button" :disabled="page <= 1" aria-label="上一页" @click="page -= 1; load()">
                上一页
              </button>
              <button
                v-for="number in pageNumbers"
                :key="number"
                type="button"
                :class="{ current: number === page }"
                @click="page = number; load()"
              >
                {{ number }}
              </button>
              <button
                type="button"
                :disabled="page >= pageCount"
                aria-label="下一页"
                @click="page += 1; load()"
              >
                下一页
              </button>
            </div>
          </div>
        </template>
      </section>
    </template>
  </div>
</template>

<style scoped>
.obj-source-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 20px;
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 16px;
  padding: 16px 20px;
}
.obj-source-title {
  color: #66788f;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 1px;
  white-space: nowrap;
}
.obj-source-item {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.obj-source-item > span {
  color: #66788f;
  font-size: 10px;
  letter-spacing: 1px;
  white-space: nowrap;
}
.obj-source-item strong {
  color: #17293f;
  font-size: 13px;
  font-weight: 700;
  white-space: nowrap;
}
.obj-source-item strong.plain {
  font-weight: 400;
}
.obj-source-item strong.mono {
  font-family: "JetBrains Mono", ui-monospace, monospace;
}
.obj-source-rule {
  width: 1px;
  height: 28px;
  background: #d9e3ef;
  flex: 0 0 1px;
}
.obj-filter {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 28px;
  padding: 2px 0 16px;
}
.obj-filter label {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 10px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1px;
  color: #66788f;
  white-space: nowrap;
}
.obj-filter select,
.obj-filter input {
  min-width: 160px;
  padding: 7px 12px;
  border: 1px solid #d9e3ef;
  border-radius: 12px;
  background: #fff;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0;
  color: #17293f;
}
.obj-filter input {
  width: 180px;
  font-weight: 400;
}
.obj-search {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 0;
  border-radius: 12px;
  background: #2e7cd6;
  color: #fff;
  padding: 7px 12px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0;
}
.obj-search svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.obj-row:hover .obj-name {
  text-decoration: underline;
}
/* 行内数值对齐对象列表原型与采集作业列表：正文 12px，Schema 与计数 11px 等宽 */
.obj-row {
  color: #17293f;
  font-size: 12px;
  font-weight: 400;
}
.obj-row > span:nth-child(2),
.obj-row > span:nth-child(4) {
  color: #66788f;
}
.obj-row > span:nth-child(2),
.obj-row > span:nth-child(5),
.obj-row > span:nth-child(6) {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
}
/* 对象列表是 7 列；共用的 .ds-jobs-row 只有 5 列，后两列会折到下一行 */
.obj-list-row {
  grid-template-columns: 88px 100px 180px minmax(0, 1fr) 84px 96px 72px;
}
.obj-list-row > span {
  min-width: 0;
}
.obj-list-row > span:nth-child(1),
.obj-list-row > span:nth-child(2),
.obj-list-row > span:nth-child(5),
.obj-list-row > span:nth-child(6),
.obj-list-row > span:nth-child(7) {
  white-space: nowrap;
}
.obj-name {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 12px;
  font-weight: 700;
  color: #17293f;
}
.ds-jobs-pager button {
  width: auto;
  min-width: 26px;
  padding: 0 8px;
}
@media (max-width: 960px) {
  .ds-jobs-table {
    overflow-x: auto;
  }
  .obj-list-row {
    min-width: 760px;
    grid-template-columns: 88px 100px 180px minmax(0, 1fr) 84px 96px 72px;
  }
}
</style>
