<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getObjectDdl, getObjectDetail, listObjectOptions } from '../api/assets'
import { displayDate } from '../api/format'
import { ApiError } from '../api/http'
import PageBreadcrumbs from '../components/PageBreadcrumbs.vue'
import type {
  ObjectDetailColumn,
  ObjectDetailResponse,
  ObjectDdlResponse,
  ObjectOptionsResponse,
} from '../api/types'

const route = useRoute()
const router = useRouter()
const sourceId = computed(() =>
  typeof route.params.sourceId === 'string' ? route.params.sourceId : '',
)
const schemaName = computed(() => String(route.query.schema_name ?? ''))
const objectName = computed(() => String(route.query.object_name ?? ''))

/** 当前对象身份：sourceId|schema|object，用作请求响应的过期判定。 */
const routeKey = computed(() => `${sourceId.value}|${schemaName.value}|${objectName.value}`)

const COLUMN_PAGE_SIZE = 10
const DEBOUNCE_MS = 250

function formatSampleValue(value: unknown): string {
  if (value === null || value === undefined) return 'NULL'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function copySql() {
  if (ddl.value?.definition_sql) {
    void navigator.clipboard?.writeText(ddl.value.definition_sql)
  }
}

const detail = ref<ObjectDetailResponse | null>(null)
const ddl = ref<ObjectDdlResponse | null>(null)
const ddlError = ref('')
const loadError = ref('')
const loading = ref(true)

const upperTab = ref<'info' | 'sql'>('info')
/** 上部卡片（基本信息 / 对象定义 SQL）是否收起内容区，仅保留 Tab 栏 */
const upperCollapsed = ref(false)
const lowerTab = ref<'dict' | 'profile' | 'sample'>('dict')
const dictPage = ref(1)
const profilePage = ref(1)

const options = ref<ObjectOptionsResponse | null>(null)
const optionsFailed = ref(false)
const cascade = ref({ objectType: '', schema: '', query: '' })
const objectCandidates = ref<{ schema_name: string; object_name: string }[]>([])

/** 候选的复合身份 schema\u0000object（NUL 不出现在 PG 标识符中，无碰撞）。 */
function candidateKey(item: { schema_name: string; object_name: string }): string {
  return `${item.schema_name}\u0000${item.object_name}`
}

const selectedObject = ref('')
const pickerOpen = ref(false)
const pickerQueryInput = ref<HTMLInputElement | null>(null)
// 请求序号：任何更新的载入都会使旧响应失效——对象身份比较覆盖不了
// A→B→A 快速往返（回到 A 后旧 A 响应仍与身份相符，doc 19.2 §3.7）
let detailRequestId = 0
let backfillRequestId = 0
let ddlRequestId = 0
let candidateRequest = 0
let debounceTimer: ReturnType<typeof setTimeout> | null = null

const profile = computed(() => detail.value?.profile ?? null)

function typeLabel(objectType: string | null | undefined): string {
  if (objectType === 'table') return '表'
  if (objectType === 'view') return '视图'
  if (objectType === 'materialized_view') return '物化视图'
  return objectType ?? '—'
}

function categoryLabel(value: string | null | undefined): string {
  if (value === 'fact') return '事实表'
  if (value === 'dim') return '维度表'
  if (value === 'bridge') return '桥接表'
  if (value === 'unknown') return '未知'
  return value ?? ''
}

function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString('zh-CN')
}

function formatGenerated(value: string | null | undefined): string {
  if (!value) return '未记录'
  return displayDate(value)
}

function formatPublished(value: string | null | undefined): string {
  if (!value) return ''
  return displayDate(value)
}

const detailErrorInconsistent = ref(false)

const sourceLineText = computed(() => {
  if (loading.value || loadError.value) return ''
  if (detailErrorInconsistent.value) return 'JSON 画像发布状态不一致，暂无法展示'
  if (!detail.value || detail.value.state !== 'ready' || !detail.value.json_publish) {
    return 'JSON 画像尚未发布'
  }
  return `发布时间：${formatPublished(detail.value.json_publish.published_at)}`
})

// ---- 基本信息（doc 19.2 §3.1，不含对象名/类型/画像生成时间；含字段数量） ----
const infoItems = computed(() => {
  const row = profile.value
  if (!row) return []
  const confidence =
    row.category_confidence === null || row.category_confidence === undefined
      ? null
      : Math.round(row.category_confidence * 100)
  // 分类值缺失时显示"未生成"（§9.1：显示实际分类或"未生成"）：
  // table_profile={} 等空画像会入库为 has_table_profile=true 且分类为 NULL，
  // 只判 has_table_profile 会渲染出空字符串
  const categoryText =
    !row.has_table_profile || !row.table_category
      ? '未生成'
      : `${categoryLabel(row.table_category)}${confidence !== null ? `(置信度${confidence}%)` : ''}`
  const pkText =
    row.primary_key_columns === null
      ? '未生成'
      : row.primary_key_columns.length === 0
        ? '无'
        : row.primary_key_columns.join('、')
  const countOr = (value: number | null | undefined) =>
    value === null || value === undefined ? '未生成' : String(value)
  return [
    { label: '数据库名', value: row.db_name },
    { label: '字段数量', value: countOr(row.column_count) },
    { label: '对象注释', value: row.object_comment || '暂无注释' },
    { label: '表分类', value: categoryText },
    { label: '主键字段', value: pkText },
    { label: '外键数量', value: countOr(row.foreign_key_count) },
    { label: '索引数量', value: countOr(row.index_count) },
    { label: '约束数', value: countOr(row.constraint_count) },
    {
      label: '采集时总行数',
      value: row.total_rows === null ? '—' : formatCount(row.total_rows),
    },
    {
      label: '画像抽样行数',
      value: row.sample_count === null ? '未记录' : formatCount(row.sample_count),
    },
  ]
})

// ---- 数据画像抽样记录数（与基本信息「画像抽样行数」一致） ----
const categoryMissing = computed(() => {
  const row = profile.value
  return !row || !row.has_table_profile || !row.table_category
})

/** 与基本信息「画像抽样行数」同源：profile.sample_count */
const profileSampleCountText = computed(() => {
  const row = profile.value
  if (!row || row.sample_count === null || row.sample_count === undefined) {
    return '未记录'
  }
  return formatCount(row.sample_count)
})

/** 与基本信息「画像生成时间」同源：profile.generated_timestamp */
const profileGeneratedTimeText = computed(() =>
  formatGenerated(profile.value?.generated_timestamp),
)

// ---- 数据字典投影（doc 19.2 §3.3） ----
/** 索引条目的统一口径（§3.3 精确匹配规则）：两表逐字一致。 */
function indexEntryText(index: Record<string, unknown>, columnName: string): string {
  const name = String(index.index_name ?? '')
  const keyColumns = (index.columns as string[] | undefined) ?? []
  const included = (index.included_columns as string[] | undefined) ?? []
  const parts: string[] = []
  if (keyColumns.includes(columnName)) parts.push('键列')
  else if (included.includes(columnName)) parts.push('INCLUDE 列')
  parts.push(index.is_unique ? '唯一' : '非唯一')
  if (index.is_primary) parts.push('支持主键约束')
  else if (index.is_constraint_backed) {
    parts.push(`支持约束：${String(index.constraint_name ?? '')}`)
  }
  return `${name}（${parts.join('；')}）`
}

function indexCell(column: ObjectDetailColumn): string {
  const indexes = column.indexes
  if (indexes === null || indexes === undefined) return '未生成'
  if (indexes.length === 0) return '无'
  return indexes.map((index) => indexEntryText(index, column.column_name)).join('\n')
}

/** 主键条目：约束名 + 完整列组，复合标"成员"（§3.3/§3.4 两表共用）。 */
function primaryKeyText(primaryKey: Record<string, unknown>): string {
  const columns = (primaryKey.columns as string[] | undefined) ?? []
  const suffix = columns.length > 1 ? '；成员' : ''
  return `${String(primaryKey.constraint_name ?? '')}（${columns.join('、')}${suffix}）`
}

/** 外键条目：约束名 + 来源列组 → 目标对象（目标列组），保持来源与目标列顺序。 */
function foreignKeyText(fk: Record<string, unknown>): string {
  const sourceColumns = (fk.source_columns as string[] | undefined) ?? []
  const targetColumns = (fk.target_columns as string[] | undefined) ?? []
  return `${String(fk.constraint_name ?? '')}（${sourceColumns.join('、')} → ${String(
    fk.target_schema ?? '',
  )}.${String(fk.target_table ?? '')}（${targetColumns.join('、')}））`
}

/** 唯一约束条目：约束名 + 完整列组，复合标"成员"。 */
function uniqueConstraintText(uq: Record<string, unknown>): string {
  const columns = (uq.columns as string[] | undefined) ?? []
  const suffix = columns.length > 1 ? '；成员' : ''
  return `${String(uq.constraint_name ?? '')}（${columns.join('、')}${suffix}）`
}

function constraintCells(column: ObjectDetailColumn) {
  const constraints = column.physical_constraints
  const pk =
    constraints.primary_key_state === 'present' && constraints.primary_key
      ? primaryKeyText(constraints.primary_key)
      : constraints.primary_key_state === 'none'
        ? '—'
        : '未生成'
  const fks = constraints.foreign_keys
  const fkText =
    fks === null || fks === undefined
      ? '未生成'
      : fks.length === 0
        ? '—'
        : fks.map((fk) => foreignKeyText(fk)).join('\n')
  const uqs = constraints.unique_constraints
  const uqText =
    uqs === null || uqs === undefined
      ? '未生成'
      : uqs.length === 0
        ? '—'
        : uqs.map((uq) => uniqueConstraintText(uq)).join('\n')
  return { pk, fkText, uqText }
}

const dictColumns = computed(() => detail.value?.columns ?? [])
const dictPageCount = computed(() =>
  Math.max(1, Math.ceil(dictColumns.value.length / COLUMN_PAGE_SIZE)),
)
const dictPageItems = computed(() => {
  const start = (dictPage.value - 1) * COLUMN_PAGE_SIZE
  return dictColumns.value.slice(start, start + COLUMN_PAGE_SIZE)
})

// ---- 数据画像 17 列（doc 19.2 §3.4） ----
const profilePageCount = computed(() =>
  Math.max(1, Math.ceil(dictColumns.value.length / COLUMN_PAGE_SIZE)),
)
const profilePageItems = computed(() => {
  const start = (profilePage.value - 1) * COLUMN_PAGE_SIZE
  return dictColumns.value.slice(start, start + COLUMN_PAGE_SIZE)
})

function uniqueSetCell(column: ObjectDetailColumn): string {
  const sets = column.unique_column_sets
  if (sets === null || sets === undefined) return '未生成'
  if (sets.length === 0) return '无'
  return sets
    .map((item) => (item.columns as string[]).join('、') + '（画像候选）')
    .join('\n')
}

function setMetricCell(column: ObjectDetailColumn, key: string): string {
  const sets = column.unique_column_sets
  if (sets === null || sets === undefined) return '未生成'
  if (sets.length === 0) return '—'
  return sets
    .map((item) => {
      const value = (item as Record<string, unknown>)[key]
      if (value === null || value === undefined) return '—'
      return typeof value === 'number' ? `${Math.round(value * 100)}%` : String(value)
    })
    .join('\n')
}

function physicalConstraintCell(column: ObjectDetailColumn): string {
  const constraints = column.physical_constraints
  const fkMissing =
    constraints.foreign_keys === null || constraints.foreign_keys === undefined
  const uqMissing =
    constraints.unique_constraints === null ||
    constraints.unique_constraints === undefined
  if (constraints.primary_key_state === 'unknown' && fkMissing && uqMissing) {
    return '未生成'
  }
  const lines: string[] = []
  if (constraints.primary_key_state === 'unknown') {
    // 主键节点缺失但外键/唯一约束节点完整：不清掉已知明细，也不把未知
    // 渲染成"无"（doc 19.2 §9.2）
    lines.push('主键：未生成')
  } else if (constraints.primary_key_state === 'present' && constraints.primary_key) {
    // 完整约束名 + 复合列组（doc 19.2 §3.4），与数据字典共用格式化
    lines.push(`主键：${primaryKeyText(constraints.primary_key)}`)
  }
  // 节点缺失（null）显式标"未生成"；完整空数组（本字段不参与）不占行——
  // 与数据字典的 null/[] 区分同口径，缺失不得被 ?? [] 收成"无"（§4/§9.2）
  if (fkMissing) {
    lines.push('外键：未生成')
  } else {
    for (const fk of constraints.foreign_keys ?? []) {
      lines.push(`外键：${foreignKeyText(fk)}`)
    }
  }
  if (uqMissing) {
    lines.push('唯一约束：未生成')
  } else {
    for (const uq of constraints.unique_constraints ?? []) {
      lines.push(`唯一约束：${uniqueConstraintText(uq)}`)
    }
  }
  if (lines.length === 0) return '—'
  return lines.join('\n')
}

function indexReferenceCell(column: ObjectDetailColumn): string {
  // 与数据字典"字段参与的索引"同一口径：索引名、键列/INCLUDE 列参与方式、
  // 唯一性及支持约束标注（doc 19.2 §3.4）
  const indexes = column.indexes
  if (indexes === null || indexes === undefined) return '未生成'
  if (indexes.length === 0) return '无'
  return indexes.map((index) => indexEntryText(index, column.column_name)).join('\n')
}

function percent(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${Math.round(value * 100)}%`
}

// ---- DDL 状态文案（doc 19.2 §3.2/§3.6） ----
// 两个 DDL Tab 的有效展示状态：取到 object-ddl 响应后以响应的 state 为准
// ——DDL 可能在详情之后重发（对象被移出发布批次等），此时详情里的
// ddl_state 已过期；详情的 ddl_state 只用于请求前的初始提示（§3.2/§3.6）
const ddlViewState = computed(() => {
  if (ddl.value) return ddl.value.state
  if (ddlError.value) return ddlError.value
  if (ddlLoading.value) return 'loading'
  return detail.value?.ddl_state ?? 'not_published'
})

function ddlAreaText(area: 'sql' | 'sample'): string {
  // 文案按渲染区域区分（doc 19.2 §3.2/§3.6）；读取失败不得误标为未发布
  const state = ddlViewState.value
  if (state === 'definition_invalid') {
    return area === 'sql'
      ? 'DDL 样例块格式异常，暂无法展示对象定义'
      : '样例数据解析失败'
  }
  if (state === 'other') {
    return area === 'sql' ? '对象定义读取失败' : '样例数据读取失败'
  }
  if (state === 'definition_inconsistent' || state === 'inconsistent') {
    return '已发布 DDL 状态不一致，暂无法展示'
  }
  if (state === 'incompatible') {
    return '当前已发布 DDL 与 JSON 对象身份不一致，暂无法展示'
  }
  return '暂无已发布 DDL 数据'
}

const ddlLoading = ref(false)
let ddlInFlightKey = ''

async function ensureDdl(retrying = false) {
  if (ddl.value || ddlError.value) return
  if (detail.value && detail.value.state !== 'ready') return
  if (detail.value && detail.value.ddl_state !== 'ready') return
  // 同一对象在途时互斥；过期响应按请求序号整笔丢弃（§3.7 不得沿用
  // 前一个对象的 SQL 或样例，A→B→A 亦覆盖）
  const key = routeKey.value
  if (ddlLoading.value && ddlInFlightKey === key) return
  ddlInFlightKey = key
  const requestId = ++ddlRequestId
  ddlLoading.value = true
  try {
    const response = await getObjectDdl(sourceId.value, schemaName.value, objectName.value)
    if (requestId !== ddlRequestId || key !== routeKey.value) return
    const displayedPublishId = detail.value?.json_publish?.publish_run_id ?? null
    if (response.json_publish_run_id !== displayedPublishId) {
      // JSON 在详情与 DDL 两次请求之间重新发布：不展示旧详情与新 DDL 的
      // 组合（doc 19.2 §8.2）——丢弃本次响应，先刷新详情；刷新后如仍停留
      // 在 DDL Tab 再取当前 DDL。连续重发布时放弃本轮，避免刷新循环
      if (retrying) {
        ddlError.value = 'other'
        return
      }
      await load()
      if (key !== routeKey.value) return
      if (upperTab.value === 'sql' || lowerTab.value === 'sample') {
        void ensureDdl(true)
      }
      return
    }
    ddl.value = response
  } catch (error) {
    if (requestId !== ddlRequestId || key !== routeKey.value) return
    if (error instanceof ApiError) {
      ddlError.value =
        error.code === 'definition_invalid' || error.code === 'definition_inconsistent'
          ? error.code
          : 'other'
    } else {
      ddlError.value = 'other'
    }
  } finally {
    if (requestId === ddlRequestId) ddlLoading.value = false
  }
}

function switchUpperTab(tab: 'info' | 'sql') {
  upperTab.value = tab
  // 收起状态下点击 Tab 意在查看该页内容，自动展开
  upperCollapsed.value = false
  if (tab === 'sql') void ensureDdl()
}

function switchLowerTab(tab: 'dict' | 'profile' | 'sample') {
  lowerTab.value = tab
  if (tab === 'sample') void ensureDdl()
}

// ---- 级联搜索栏（doc 19.2 §3.7） ----
async function loadOptionsForBackfill() {
  const requestId = ++backfillRequestId
  optionsFailed.value = false
  try {
    const response = await listObjectOptions(sourceId.value)
    if (requestId !== backfillRequestId) return
    options.value = response
    if (response.state !== 'ready') {
      objectCandidates.value = []
      return
    }
    const current = response.objects.find(
      (item) => item.schema_name === schemaName.value && item.object_name === objectName.value,
    )
    cascade.value.objectType = current?.object_type ?? ''
    const schemas = narrowSchemas(response, cascade.value.objectType)
    cascade.value.schema = schemas.includes(schemaName.value) ? schemaName.value : ''
    // 先设 Schema 再刷新候选（按类型+Schema 收窄），跨 Schema 同名对象
    // 时才不会回填出歧义候选；选择值用复合身份（doc 19.2 §3.7）
    await refreshCandidates()
    if (requestId !== backfillRequestId) return
    selectedObject.value = candidateKey({
      schema_name: schemaName.value,
      object_name: objectName.value,
    })
  } catch {
    if (requestId === backfillRequestId) {
      options.value = null
      optionsFailed.value = true
    }
  }
}

/** 按类型收窄 Schema 候选（doc 19.2 §3.7：二级只列该类型下有对象的 Schema）。

从同一份全量候选中客户端过滤（候选项自带 object_type），与后端
object_type 收窄语义一致且不额外发请求；顺序沿用后端稳定排序。
*/
function narrowSchemas(
  response: ObjectOptionsResponse | null,
  objectType: string,
): string[] {
  if (!response || response.state !== 'ready') return []
  if (!objectType) return response.schemas
  return response.schemas.filter((name) =>
    response.objects.some(
      (item) => item.object_type === objectType && item.schema_name === name,
    ),
  )
}

/** 二级 SCHEMA 下拉选项：随当前所选类型收窄。 */
const schemaChoices = computed(() => narrowSchemas(options.value, cascade.value.objectType))

async function refreshCandidates() {
  const requestId = ++candidateRequest
  try {
    const response = await listObjectOptions(sourceId.value, {
      object_type: cascade.value.objectType || undefined,
      schema_name: cascade.value.schema || undefined,
      keyword: cascade.value.query || undefined,
    })
    if (requestId !== candidateRequest) return
    objectCandidates.value =
      response.state === 'ready'
        ? response.objects.map((item) => ({
            schema_name: item.schema_name,
            object_name: item.object_name,
          }))
        : []
  } catch {
    if (requestId === candidateRequest) objectCandidates.value = []
  }
}

function clearCandidateDebounce() {
  if (debounceTimer) {
    clearTimeout(debounceTimer)
    debounceTimer = null
  }
}

function onTypeChange() {
  cascade.value.schema = ''
  cascade.value.query = ''
  selectedObject.value = ''
  pickerOpen.value = false
  clearCandidateDebounce()
  void refreshCandidates()
}

function onSchemaChange() {
  cascade.value.query = ''
  selectedObject.value = ''
  pickerOpen.value = false
  clearCandidateDebounce()
  void refreshCandidates()
}

const optionsReady = computed(() => options.value?.state === 'ready')

/** 关闭态展示已选对象；未选 Schema 时带上 Schema，避免同名对象分不清。 */
const pickerLabel = computed(() => {
  const sep = selectedObject.value.indexOf('\u0000')
  if (sep < 0) return ''
  const schema = selectedObject.value.slice(0, sep)
  const name = selectedObject.value.slice(sep + 1)
  return cascade.value.schema ? name : `${schema}.${name}`
})

function candidateLabel(item: { schema_name: string; object_name: string }): string {
  return cascade.value.schema ? item.object_name : `${item.schema_name}.${item.object_name}`
}

function closePicker() {
  pickerOpen.value = false
}

async function togglePicker() {
  if (!optionsReady.value) return
  if (pickerOpen.value) {
    closePicker()
    return
  }
  pickerOpen.value = true
  cascade.value.query = ''
  void refreshCandidates()
  await nextTick()
  pickerQueryInput.value?.focus()
}

function onPickerFocusOut(event: FocusEvent) {
  const next = event.relatedTarget
  if (next instanceof Node && (event.currentTarget as HTMLElement).contains(next)) return
  closePicker()
}

function pickCandidate(item: { schema_name: string; object_name: string }) {
  selectedObject.value = candidateKey(item)
  cascade.value.query = ''
  closePicker()
}

function onCandidateInput(event: Event) {
  cascade.value.query = (event.target as HTMLInputElement).value
  // 文字变化即清除已选身份并立即失效在途候选请求（doc 19.2 §3.7）：
  // 自由文本不充当对象身份——旧选择不得在候选变化后继续可提交，
  // 防抖窗口内返回的旧候选也不得回填
  selectedObject.value = ''
  candidateRequest += 1
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => void refreshCandidates(), DEBOUNCE_MS)
}

function searchObject() {
  closePicker()
  if (!selectedObject.value) return
  const target = objectCandidates.value.find(
    (item) => candidateKey(item) === selectedObject.value,
  )
  if (!target) return
  router.replace({
    path: `/data-assets/${sourceId.value}/objects/detail`,
    query: { schema_name: target.schema_name, object_name: target.object_name },
  })
}

const optionsEmptyText = computed(() => {
  if (!options.value) return optionsFailed.value ? '候选加载失败' : ''
  if (options.value.state === 'not_published') return '尚未发布 JSON 画像，暂无可选对象'
  if (options.value.state === 'inconsistent') return 'JSON 画像发布状态不一致，暂无法选择对象'
  return ''
})

// ---- 载入与重置（doc 19.2 §3.7） ----
async function load() {
  const requestId = ++detailRequestId
  loading.value = true
  loadError.value = ''
  detailErrorInconsistent.value = false
  detail.value = null
  ddl.value = null
  ddlError.value = ''
  ddlLoading.value = false
  // 使在途 DDL 请求立即失效：其响应不得作为新对象的缓存写入（doc 19.2
  // §3.7，否则打开新对象 SQL/样例 Tab 会命中旧对象缓存直接返回）
  ddlRequestId += 1
  try {
    const response = await getObjectDetail(sourceId.value, schemaName.value, objectName.value)
    if (requestId !== detailRequestId) return
    detail.value = response
  } catch (error) {
    if (requestId !== detailRequestId) return
    if (error instanceof ApiError && error.code === 'profile_inconsistent') {
      detailErrorInconsistent.value = true
    } else if (
      error instanceof ApiError &&
      (error.code === 'not_found' || error.code === 'invalid_request')
    ) {
      loadError.value = '对象不存在或参数不正确'
    } else {
      loadError.value = error instanceof ApiError ? error.message : '对象详情加载失败'
    }
  } finally {
    if (requestId === detailRequestId) loading.value = false
  }
}

watch(
  [sourceId, schemaName, objectName],
  () => {
    if (!schemaName.value || !objectName.value) return
    upperTab.value = 'info'
    lowerTab.value = 'dict'
    dictPage.value = 1
    profilePage.value = 1
    void load()
    void loadOptionsForBackfill()
  },
  { immediate: true },
)

watch(
  sourceId,
  () => {
    cascade.value = { objectType: '', schema: '', query: '' }
    selectedObject.value = ''
    pickerOpen.value = false
    void loadOptionsForBackfill()
  },
  { immediate: true },
)

function goBack() {
  // doc 9 §7.4：固定返回该数据源的对象列表（保留 schema_name），不再读浏览器历史
  void router.push({
    path: `/data-assets/${sourceId.value}/objects`,
    query: schemaName.value ? { schema_name: schemaName.value } : {},
  })
}

const dictTable = ref<HTMLTableElement | null>(null)
let dictStickyObserver: ResizeObserver | null = null

/** 首列按标题收缩后，把第二列的钉住位置对齐到首列右缘。 */
function syncDictStickyColumn() {
  dictStickyObserver?.disconnect()
  dictStickyObserver = null
  const table = dictTable.value
  const header = table?.querySelector('thead th.col-fit')
  if (!table || !(header instanceof HTMLElement)) return
  const apply = () => {
    table.style.setProperty('--obj-sticky-1', `${header.offsetWidth}px`)
  }
  apply()
  dictStickyObserver = new ResizeObserver(apply)
  dictStickyObserver.observe(header)
}

watch(
  () => lowerTab.value === 'dict' && detail.value !== null,
  (visible) => {
    if (!visible) {
      dictStickyObserver?.disconnect()
      dictStickyObserver = null
      return
    }
    void nextTick(syncDictStickyColumn)
  },
  { flush: 'post' },
)

onBeforeUnmount(() => {
  clearCandidateDebounce()
  dictStickyObserver?.disconnect()
})
</script>

<template>
  <div class="ds-jobs obj-detail">
    <PageBreadcrumbs
      :segments="[
        { label: '数据资产', to: '/data-assets' },
        // 本页不加载源显示名，按 §6.1 以「数据源」占位
        { label: '数据源', to: `/data-assets/${sourceId}/profile` },
        {
          label: '对象列表',
          // query 对象构造（与返回按钮一致）：schema 名可含 &、# 等特殊字符
          to: {
            path: `/data-assets/${sourceId}/objects`,
            query: schemaName ? { schema_name: schemaName } : {},
          },
        },
        { label: `${schemaName}.${objectName}` },
      ]"
    />
    <header class="ds-jobs-header">
      <div>
        <span class="ds-kicker">OBJECT PROFILE</span>
        <h1>{{ schemaName }}.{{ objectName }}</h1>
        <p class="obj-source-line obj-header-meta">
          <template v-if="profile">对象类型：{{ typeLabel(profile.object_type) }} · </template>
          <span>{{ sourceLineText }}</span>
        </p>
      </div>
      <button type="button" class="ds-jobs-back" @click="goBack">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
        返回
      </button>
    </header>

    <!-- 三级级联搜索栏（14.1 原型；doc 19.2 §3.7） -->
    <section class="ds-jobs-card obj-cascade">
      <div class="obj-filter">
        <label>
          <span>对象类型</span>
          <select :value="cascade.objectType" @change="cascade.objectType = ($event.target as HTMLSelectElement).value; onTypeChange()">
            <option value="">全部</option>
            <option
              v-for="itemType in options?.object_types ?? []"
              :key="itemType"
              :value="itemType"
            >
              {{ typeLabel(itemType) }}
            </option>
          </select>
        </label>
        <label>
          <span>SCHEMA</span>
          <select :value="cascade.schema" @change="cascade.schema = ($event.target as HTMLSelectElement).value; onSchemaChange()">
            <option value="">全部</option>
            <option v-for="name in schemaChoices" :key="name" :value="name">
              {{ name }}
            </option>
          </select>
        </label>
        <div class="obj-picker" @focusout="onPickerFocusOut">
          <span>对象名称</span>
          <div class="obj-picker-control">
            <button
              type="button"
              class="obj-picker-trigger"
              :aria-expanded="pickerOpen"
              aria-haspopup="listbox"
              :disabled="!optionsReady"
              @click="togglePicker"
            >
              <span :class="{ placeholder: !pickerLabel }">{{ pickerLabel || '请选择对象' }}</span>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
            </button>
            <div v-if="pickerOpen" class="obj-picker-panel" role="listbox">
              <label class="obj-picker-search">
                <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
                <input
                  ref="pickerQueryInput"
                  type="text"
                  :value="cascade.query"
                  placeholder="搜索表名…"
                  @input="onCandidateInput"
                  @keydown.esc.prevent="closePicker"
                />
              </label>
              <p v-if="objectCandidates.length === 0" class="obj-picker-empty">没有匹配的对象</p>
              <ul v-else>
                <li v-for="item in objectCandidates" :key="candidateKey(item)">
                  <button
                    type="button"
                    role="option"
                    :aria-selected="candidateKey(item) === selectedObject"
                    :class="{ active: candidateKey(item) === selectedObject }"
                    @mousedown.prevent="pickCandidate(item)"
                  >
                    {{ candidateLabel(item) }}
                  </button>
                </li>
              </ul>
            </div>
          </div>
        </div>
        <button type="button" class="obj-search" :disabled="!selectedObject" @click="searchObject">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
          搜索
        </button>
      </div>
      <p v-if="optionsEmptyText" class="ds-jobs-empty">{{ optionsEmptyText }}</p>
    </section>

    <p v-if="loading" class="ds-jobs-state">正在读取对象详情…</p>
    <p v-else-if="loadError" class="ds-jobs-state error">{{ loadError }}</p>

    <template v-else>
      <!-- 409 profile_inconsistent：正文只保留错误说明（doc 19.2 §3） -->
      <section v-if="detailErrorInconsistent" class="ds-jobs-card">
        <p class="ds-jobs-empty">JSON 画像发布状态不一致，暂无法展示；请稍后重试或重新发布。</p>
      </section>

      <!-- HTTP 200 not_published：两组 Tab 区域显示空态（doc 19.2 §3） -->
      <template v-else-if="detail && detail.state === 'not_published'">
        <section class="ds-jobs-card">
          <p class="ds-jobs-empty">JSON 画像尚未发布</p>
        </section>
        <section class="ds-jobs-card">
          <p class="ds-jobs-empty">JSON 画像尚未发布</p>
        </section>
      </template>

      <template v-else-if="detail && detail.state === 'ready' && profile">
        <!-- 上部两组 Tab 之一：基本信息 / 对象定义 SQL -->
        <section class="ds-jobs-card" :class="{ 'obj-upper-collapsed': upperCollapsed }">
          <div class="obj-tabs" role="tablist">
            <button
              type="button"
              role="tab"
              :class="{ active: upperTab === 'info' }"
              @click="switchUpperTab('info')"
            >
              基本信息
            </button>
            <button
              type="button"
              role="tab"
              :class="{ active: upperTab === 'sql' }"
              @click="switchUpperTab('sql')"
            >
              对象定义 SQL
            </button>
            <button
              type="button"
              class="obj-collapse-btn"
              :class="{ collapsed: upperCollapsed }"
              :aria-expanded="!upperCollapsed"
              :title="upperCollapsed ? '展开' : '收起'"
              :aria-label="upperCollapsed ? '展开' : '收起'"
              @click="upperCollapsed = !upperCollapsed"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m18 15-6-6-6 6" /></svg>
            </button>
          </div>

          <div v-if="!upperCollapsed && upperTab === 'info'" class="obj-info-grid">
            <p v-for="item in infoItems" :key="item.label" class="obj-info-item">
              <span>{{ item.label }}</span>
              <strong>{{ item.value }}</strong>
            </p>
          </div>

          <div v-else-if="!upperCollapsed" class="obj-sql">
            <p v-if="ddlViewState === 'loading'" class="ds-jobs-state">正在读取对象定义…</p>
            <template v-else-if="ddlViewState === 'ready' && ddl">
              <p class="obj-source-line">
                DDL 加载时间：{{ formatPublished(ddl.ddl_publish?.loaded_at) }}
              </p>
              <pre class="obj-code">{{ ddl.definition_sql }}</pre>
              <button type="button" class="button button-outline" @click="copySql">
                复制 SQL
              </button>
            </template>
            <p v-else class="ds-jobs-empty">{{ ddlAreaText('sql') }}</p>
          </div>
        </section>

        <!-- 下部三 Tab：数据字典 / 数据画像 / 样例数据 -->
        <section class="ds-jobs-card obj-lower-panel">
          <div class="obj-tabs" role="tablist">
            <button
              type="button"
              role="tab"
              :class="{ active: lowerTab === 'dict' }"
              @click="switchLowerTab('dict')"
            >
              数据字典
            </button>
            <button
              type="button"
              role="tab"
              :class="{ active: lowerTab === 'profile' }"
              @click="switchLowerTab('profile')"
            >
              数据画像
            </button>
            <button
              type="button"
              role="tab"
              :class="{ active: lowerTab === 'sample' }"
              @click="switchLowerTab('sample')"
            >
              样例数据
            </button>
          </div>

          <!-- 数据字典：11 列（doc 19.2 §3.3） -->
          <div v-if="lowerTab === 'dict'" class="obj-dict-panel">
            <p class="obj-column-count">字段数量：{{ profile.column_count }}</p>
            <div class="obj-table-wrap">
              <table ref="dictTable" class="obj-table obj-dict-table">
              <thead>
                <tr>
                  <th class="sticky-col col-fit">字段序号</th>
                  <th class="sticky-col sticky-2">字段名</th>
                  <th>数据类型</th>
                  <th>字段注释</th>
                  <th>注释来源</th>
                  <th class="col-fit">可空</th>
                  <th>默认值</th>
                  <th>主键约束</th>
                  <th>外键约束与目标</th>
                  <th>唯一约束</th>
                  <th>字段参与的索引</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="column in dictPageItems" :key="column.column_name">
                  <td class="sticky-col col-fit">{{ column.ordinal_position }}</td>
                  <td class="sticky-col sticky-2">{{ column.column_name }}</td>
                  <td>{{ column.data_type }}</td>
                  <td>{{ column.column_comment || '暂无注释' }}</td>
                  <td>{{ column.comment_source || '未标注' }}</td>
                  <td class="col-fit">{{ column.is_nullable ? '是' : '否' }}</td>
                  <td>{{ column.column_default ?? '—' }}</td>
                  <td>{{ constraintCells(column).pk }}</td>
                  <td>{{ constraintCells(column).fkText }}</td>
                  <td>{{ constraintCells(column).uqText }}</td>
                  <td>{{ indexCell(column) }}</td>
                </tr>
              </tbody>
            </table>
            <div v-if="dictPageCount > 1" class="ds-jobs-pager">
              <span>共 {{ dictColumns.length }} 个字段 · 第 {{ dictPage }} / {{ dictPageCount }} 页</span>
              <div>
                <button type="button" :disabled="dictPage <= 1" @click="dictPage -= 1">上一页</button>
                <button type="button" :disabled="dictPage >= dictPageCount" @click="dictPage += 1">下一页</button>
              </div>
            </div>
            </div>
          </div>

          <!-- 数据画像：抽样记录数 + 17 列字段表（doc 19.2 §3.4） -->
          <div v-else-if="lowerTab === 'profile'" class="obj-profile-panel">
            <p v-if="categoryMissing" class="ds-jobs-empty">表分类未生成</p>
            <p class="obj-column-count">抽样记录数：{{ profileSampleCountText }}</p>
            <div class="obj-table-wrap">
            <!-- 字段统计与语义列来自 column_profiles，与 table_profile 无关：
                 has_table_profile=false 时表格仍渲染，仅结构参照列显示“未生成”（§3.4/§9.2） -->
            <table class="obj-table">
              <thead>
                <tr>
                  <th class="sticky-col">字段序号</th>
                  <th class="sticky-col sticky-2">字段名</th>
                  <th>物理约束（数据库结构）</th>
                  <th>字段参与的索引参照（数据库结构）</th>
                  <th>候选唯一列组</th>
                  <th>候选置信度</th>
                  <th>候选唯一度</th>
                  <th>候选空值率</th>
                  <th>语义角色</th>
                  <th>语义置信度</th>
                  <th>语义判断依据</th>
                  <th>抽样空值数</th>
                  <th>抽样空值率</th>
                  <th>抽样不同值数</th>
                  <th>抽样唯一度</th>
                  <th>抽样最小值</th>
                  <th>抽样最大值</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="column in profilePageItems" :key="column.column_name">
                  <td class="sticky-col">{{ column.ordinal_position }}</td>
                  <td class="sticky-col sticky-2">{{ column.column_name }}</td>
                  <td>{{ physicalConstraintCell(column) }}</td>
                  <td>{{ indexReferenceCell(column) }}</td>
                  <td>{{ uniqueSetCell(column) }}</td>
                  <td>{{ setMetricCell(column, 'confidence_score') }}</td>
                  <td>{{ setMetricCell(column, 'uniqueness') }}</td>
                  <td>{{ setMetricCell(column, 'null_rate') }}</td>
                  <td>{{ column.semantic_role || '未识别' }}</td>
                  <td>{{ percent(column.semantic_confidence) }}</td>
                  <td>{{ column.semantic_basis?.join('、') || '—' }}</td>
                  <td>{{ column.null_count ?? '—' }}</td>
                  <td>{{ percent(column.null_rate) }}</td>
                  <td>{{ column.unique_count ?? '—' }}</td>
                  <td>{{ percent(column.uniqueness) }}</td>
                  <td>{{ column.sample_min ?? '—' }}</td>
                  <td>{{ column.sample_max ?? '—' }}</td>
                </tr>
              </tbody>
            </table>
            <div v-if="profilePageCount > 1" class="ds-jobs-pager">
              <span>共 {{ dictColumns.length }} 个字段 · 第 {{ profilePage }} / {{ profilePageCount }} 页</span>
              <div>
                <button type="button" :disabled="profilePage <= 1" @click="profilePage -= 1">上一页</button>
                <button type="button" :disabled="profilePage >= profilePageCount" @click="profilePage += 1">下一页</button>
              </div>
            </div>
            </div>
          </div>

          <!-- 样例数据（doc 19.2 §3.6） -->
          <div v-else class="obj-sample-panel">
            <p class="obj-column-count">样例数据采集时间：{{ profileGeneratedTimeText }}</p>
            <p v-if="ddlViewState === 'loading'" class="ds-jobs-state">正在读取样例数据…</p>
            <template v-else-if="ddlViewState === 'ready' && ddl">
              <p v-if="ddl.sample_state === 'no_sample_block'" class="ds-jobs-empty">
                本次已发布 DDL 未包含样例数据
              </p>
              <p v-else-if="ddl.sample_state === 'empty'" class="ds-jobs-empty">
                本次已发布 DDL 包含 0 条样例记录
              </p>
              <div v-else class="obj-table-wrap">
                <table class="obj-table">
                  <thead>
                    <tr>
                      <th v-for="column in ddl.sample_columns" :key="column">{{ column }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(record, index) in ddl.sample_records" :key="index">
                      <td v-for="column in ddl.sample_columns" :key="column">
                        {{ formatSampleValue((record as Record<string, unknown>)[column]) }}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </template>
            <p v-else class="ds-jobs-empty">{{ ddlAreaText('sample') }}</p>
          </div>
        </section>
      </template>
    </template>
  </div>
</template>

<style scoped>
.obj-cascade {
  padding: 16px 20px;
  gap: 10px;
}
.obj-filter {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 28px;
}
.obj-filter label,
.obj-picker {
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
.obj-picker-trigger {
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
.obj-picker {
  position: relative;
}
.obj-picker-control {
  position: relative;
}
.obj-picker-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 180px;
  cursor: pointer;
}
.obj-picker-trigger .placeholder {
  font-weight: 400;
  color: #8a99ac;
}
.obj-picker-trigger svg,
.obj-picker-search svg,
.obj-search svg {
  width: 14px;
  height: 14px;
  flex: 0 0 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.obj-picker-trigger svg {
  color: #66788f;
}
.obj-picker-panel {
  position: absolute;
  z-index: 20;
  top: calc(100% + 6px);
  left: 0;
  width: 240px;
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 12px;
  box-shadow: 0 8px 24px rgba(23, 41, 63, 0.1);
  padding: 8px;
}
.obj-picker-search {
  display: flex;
  align-items: center;
  gap: 6px;
  border: 1px solid #d9e3ef;
  border-radius: 8px;
  padding: 6px 8px;
  color: #66788f;
}
.obj-picker-search input {
  width: 100%;
  min-width: 0;
  border: 0;
  padding: 0;
  outline: none;
  background: transparent;
  font-size: 12px;
  font-weight: 400;
  letter-spacing: 0;
  color: #17293f;
}
.obj-picker-panel ul {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  max-height: 220px;
  overflow: auto;
}
.obj-picker-panel li button {
  width: 100%;
  text-align: left;
  border: 0;
  background: transparent;
  border-radius: 8px;
  padding: 7px 10px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0;
  color: #17293f;
  cursor: pointer;
}
.obj-picker-panel li button.active,
.obj-picker-panel li button:hover {
  background: #e3eefb;
  color: #2e7cd6;
}
.obj-picker-empty {
  margin: 8px 0 0;
  padding: 8px 10px;
  color: #66788f;
  font-size: 12px;
  font-weight: 400;
  letter-spacing: 0;
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
.obj-search:disabled {
  opacity: 0.45;
}
.obj-source-line {
  margin-top: 6px;
  font-size: 13px;
  color: var(--ds-muted, #64748b);
}
.obj-header-meta {
  font-size: 11px;
  line-height: 1.45;
}
.obj-tabs {
  display: flex;
  gap: 8px;
  border-bottom: 1px solid var(--ds-border, #e2e8f0);
  margin-bottom: 16px;
}
.obj-tabs button {
  padding: 8px 16px;
  border: none;
  background: transparent;
  font-size: 14px;
  color: var(--ds-muted, #64748b);
  cursor: pointer;
  border-bottom: 2px solid transparent;
}
.obj-tabs button.active {
  color: var(--ds-ink, #0f172a);
  border-bottom-color: var(--ds-accent, #2563eb);
  font-weight: 600;
}
.obj-tabs button.obj-collapse-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  margin: 0 0 6px auto;
  align-self: center;
  border: none;
  border-radius: 6px;
  background: #eef3f9;
  color: #66788f;
}
.obj-collapse-btn svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform 0.15s;
}
.obj-collapse-btn.collapsed svg {
  transform: rotate(180deg);
}
.obj-upper-collapsed .obj-tabs {
  margin-bottom: 0;
}
.obj-lower-panel {
  gap: 0;
}
.obj-lower-panel .obj-tabs {
  margin-bottom: 0;
}
.obj-dict-panel,
.obj-profile-panel,
.obj-sample-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 10px;
}
.obj-column-count {
  margin: 0;
  color: #66788f;
  font-size: 12px;
  font-weight: 400;
  line-height: 1.4;
}
.obj-info-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0 24px;
  /* 行间距 = 上下各 6px 内边距（与原 12px 行距一致）；末行分隔线被裁掉，
     负外边距抵消首尾内边距，整体占位不变 */
  margin: -6px 0;
  clip-path: inset(0 0 6px 0);
}
.obj-info-item {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 6px 0;
  font-size: 13px;
}
/* 设计稿 16：每行下方一条 #D9E3EF 横线，跨列连续 */
.obj-info-item::after {
  content: '';
  position: absolute;
  left: 0;
  right: -24px;
  bottom: 0;
  height: 1px;
  background: #d9e3ef;
}
.obj-info-item:nth-child(3n)::after {
  right: 0;
}
.obj-info-item span {
  color: var(--ds-muted, #64748b);
  font-size: 12px;
}
.obj-info-item strong {
  font-weight: 400;
}
.obj-code {
  max-height: 480px;
  overflow: auto;
  padding: 12px;
  background: #0f172a;
  color: #e2e8f0;
  border-radius: 8px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  white-space: pre;
}
.obj-table-wrap {
  overflow-x: auto;
}
.obj-table {
  /* 固定首列（字段序号）宽度：第二列钉住偏移取同一变量，滚动时两列
     不重叠（宽度不定时钉点会落在首列内部，字段名盖住序号） */
  --obj-sticky-1: 96px;
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.obj-table th,
.obj-table td {
  border: 1px solid var(--ds-border, #e2e8f0);
  padding: 8px 10px;
  text-align: left;
  vertical-align: top;
  white-space: pre-line;
  min-width: 96px;
}
.obj-table th {
  background: #f8fafc;
  position: sticky;
  top: 0;
}
.sticky-col {
  position: sticky;
  left: 0;
  box-sizing: border-box;
  width: var(--obj-sticky-1);
  min-width: var(--obj-sticky-1);
  max-width: var(--obj-sticky-1);
  background: #fff;
  z-index: 1;
}
.sticky-2 {
  /* 钉在首列右缘：与首列实际宽度（同一边界盒模型）逐像素对齐 */
  left: var(--obj-sticky-1);
  width: auto;
  max-width: none;
  z-index: 2;
  /* 钉住边界提示：内容从两列下方滚过 */
  box-shadow: 6px 0 6px -6px rgba(15, 23, 42, 0.16);
}
.obj-table th.sticky-col {
  background: #f1f5f9;
  z-index: 3;
}
/* 数据字典：这两列只按标题单行所需宽度展开，不套用全表 96px 下限 */
.obj-dict-table :is(th, td).col-fit {
  width: 1px;
  min-width: max-content;
  max-width: none;
  white-space: nowrap;
}
.obj-notice {
  padding: 10px 12px;
  background: #f8fafc;
  border-left: 3px solid var(--ds-accent, #2563eb);
  font-size: 12px;
  color: #475569;
}
@media (max-width: 720px) {
  .obj-info-grid {
    grid-template-columns: 1fr;
  }
  .obj-info-item::after {
    right: 0;
  }
}
</style>
