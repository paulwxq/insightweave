<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import {
  getRelConfig,
  getRelObjectColumns,
  putRelConfig,
} from '../api/dataSources'
import { ApiError } from '../api/http'
import type {
  RelConfigRelationship,
  RelConfigRelationshipInput,
} from '../api/types'
import { shownAtCqlThreshold } from '../utils/relDisplayFilter'

// doc 23.2 §3.3（原型 23.rel-1~5）：
// - 列表态（rel-1）：统计标签按 discovery_method 派生 + 五列表格
//   （只展示外键，以及 composite_score >= cql_generation.composite_score_threshold
//   的关系；未达阈值的条目不出现，保存时原样附回）+ 「+ 新增关系」虚线入口；
// - 修改态（rel-2）/新增态（rel-4）：共用表单；有意偏离原型——列名为多选
//   chips 按序配对（复合键），基数 {N:1, 1:1, M:N}（reader 拒 1:N）。
//   左侧固定为箭头起点、右侧固定为终点；「互换两侧」对调表与列，基数不变。
// - 删除确认（rel-3）独立小弹窗；保存成功卡片（rel-5）含「x 条修改 ·
//   y 条删除 · z 条新增」（前端对 GET 基线 diff）与「查看 rel json 文件」。

interface Substance {
  fromSchema: string
  fromTable: string
  fromCols: string[]
  toSchema: string
  toTable: string
  toCols: string[]
  cardinality: string
}

interface DraftRow extends Substance {
  key: string
  id: string | null
  method: string // 基线 discovery_method（未变更条目的展示值；编辑表单会改写成 manual）
  score: number | null // 基线 composite_score（编辑表单会改写成 1）
  baselineMethod: string // 入列时的发现方式，展示过滤只用它
  baselineScore: number | null
  orig: Substance | null // 基线实质字段（变更判定）
}

const props = defineProps<{
  sourceId: string
  jobId: string
}>()

const emit = defineEmits<{
  close: []
  saved: []
}>()

const CARDINALITIES = ['N:1', '1:1', 'M:N'] as const
const INFERRED_METHODS = new Set([
  'physical_key_matching',
  'logical_key_matching',
  'llm_inferred',
])
const METHOD_LABELS: Record<string, string> = {
  foreign_key_constraint: '外键约束',
  physical_key_matching: '物理键匹配',
  logical_key_matching: '逻辑键匹配',
  llm_inferred: 'LLM 推断',
  manual: '手动新增',
}

const loading = ref(true)
const fatal = ref('')
const database = ref('')
const fingerprint = ref('')
const scoreThreshold = ref(0.9)
const tables = ref<string[]>([])
const draft = ref<DraftRow[]>([])
const rawBaseline = ref<RelConfigRelationship[]>([])

const formOpen = ref(false)
const formIsEdit = ref(false)
const formError = ref('')
const srcTable = ref('')
const dstTable = ref('')
const srcCols = ref<string[]>([])
const dstCols = ref<string[]>([])
const cardinality = ref('N:1')
const editingKey = ref('')
const columnsCache = ref(new Map<string, string[]>())
const columnsLoading = ref('')
const pendingDelete = ref<DraftRow | null>(null)
const tablePicker = ref<'' | 'src' | 'dst'>('')
const tableQuery = ref('')
const tableQueryInput = ref<HTMLInputElement | null>(null)

const saving = ref(false)
const saveError = ref('')
const saveStale = ref(false)
const savedCard = ref<{ modified: number; removed: number; added: number } | null>(
  null,
)
const jsonPreviewOpen = ref(false)

let keySeq = 0

function substanceOf(row: DraftRow): Substance {
  return {
    fromSchema: row.fromSchema,
    fromTable: row.fromTable,
    fromCols: [...row.fromCols],
    toSchema: row.toSchema,
    toTable: row.toTable,
    toCols: [...row.toCols],
    cardinality: row.cardinality,
  }
}

function sameSubstance(a: Substance, b: Substance | null): boolean {
  if (!b) return false
  return (
    a.fromSchema === b.fromSchema &&
    a.fromTable === b.fromTable &&
    a.fromCols.join('\u0000') === b.fromCols.join('\u0000') &&
    a.toSchema === b.toSchema &&
    a.toTable === b.toTable &&
    a.toCols.join('\u0000') === b.toCols.join('\u0000') &&
    a.cardinality === b.cardinality
  )
}

function rowChanged(row: DraftRow): boolean {
  return !sameSubstance(substanceOf(row), row.orig)
}

function effectiveMethod(row: DraftRow): string {
  if (row.id === null || rowChanged(row)) return 'manual'
  return row.method
}

function effectiveScore(row: DraftRow): number {
  if (row.id === null || rowChanged(row)) return 1.0
  return row.score ?? 1.0
}

function rowShown(row: DraftRow): boolean {
  return shownAtCqlThreshold(
    row.baselineMethod,
    row.baselineScore,
    scoreThreshold.value,
  )
}

function entryFromRaw(raw: RelConfigRelationship, index: number): DraftRow {
  const fromTable = raw.from_table as { schema: string; table: string }
  const toTable = raw.to_table as { schema: string; table: string }
  const fromCols = Array.isArray(raw.from_columns)
    ? (raw.from_columns as string[])
    : [raw.from_column as string]
  const toCols = Array.isArray(raw.to_columns)
    ? (raw.to_columns as string[])
    : [raw.to_column as string]
  const row: DraftRow = {
    key: `base-${index}`,
    id: String(raw.relationship_id),
    fromSchema: fromTable.schema,
    fromTable: fromTable.table,
    fromCols,
    toSchema: toTable.schema,
    toTable: toTable.table,
    toCols,
    cardinality: String(raw.cardinality ?? 'N:1'),
    method: String(raw.discovery_method ?? 'manual'),
    score:
      typeof raw.composite_score === 'number' ? raw.composite_score : null,
    baselineMethod: String(raw.discovery_method ?? 'manual'),
    baselineScore:
      typeof raw.composite_score === 'number' ? raw.composite_score : null,
    orig: null,
  }
  row.orig = substanceOf(row)
  return row
}

async function load() {
  loading.value = true
  fatal.value = ''
  try {
    const data = await getRelConfig(props.sourceId, props.jobId)
    database.value = data.database
    fingerprint.value = data.fingerprint
    scoreThreshold.value = data.score_threshold
    tables.value = data.tables
    rawBaseline.value = data.relationships
    draft.value = data.relationships.map((raw, index) => entryFromRaw(raw, index))
  } catch (error) {
    fatal.value = error instanceof ApiError ? error.message : '读取 Rel 产物失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)

const visibleRows = computed(() => draft.value.filter(rowShown))

const stats = computed(() => {
  const rows = visibleRows.value
  const total = rows.length
  let fk = 0
  let inferred = 0
  let manual = 0
  for (const row of rows) {
    const method = effectiveMethod(row)
    if (method === 'foreign_key_constraint') fk += 1
    else if (INFERRED_METHODS.has(method)) inferred += 1
    else manual += 1
  }
  return { total, fk, inferred, manual }
})

const diffCounts = computed(() => {
  const rows = visibleRows.value
  let modified = 0
  let added = 0
  for (const row of rows) {
    if (row.id === null) added += 1
    else if (rowChanged(row)) modified += 1
  }
  const draftIds = new Set(rows.filter((r) => r.id !== null).map((r) => r.id))
  const removed = rawBaseline.value.filter((raw) => {
    const method = String(raw.discovery_method ?? 'manual')
    const score =
      typeof raw.composite_score === 'number' ? raw.composite_score : null
    if (!shownAtCqlThreshold(method, score, scoreThreshold.value)) return false
    return !draftIds.has(String(raw.relationship_id))
  }).length
  return { modified, removed, added }
})

// 表单打开时禁用：底部保存只提交 draft，表单输入须先经表单内
// 「保存修改／+ 保存关系」入列——否则未提交的编辑会被静默丢弃（假成功）。
// 相对打开时的可见列表没有任何增删改时也禁用，避免空保存重写 JSON 与 MD。
const hasEdits = computed(
  () =>
    diffCounts.value.modified + diffCounts.value.removed + diffCounts.value.added > 0,
)
const canSave = computed(
  () =>
    !loading.value &&
    !fatal.value &&
    !saving.value &&
    !savedCard.value &&
    !formOpen.value &&
    hasEdits.value,
)

// ---- 表单 ----

async function ensureColumns(tableKey: string): Promise<string[]> {
  if (!tableKey) return []
  const cached = columnsCache.value.get(tableKey)
  if (cached) return cached
  const [schema, table] = tableKey.split('.', 2)
  columnsLoading.value = tableKey
  try {
    const data = await getRelObjectColumns(props.sourceId, props.jobId, schema, table)
    columnsCache.value.set(tableKey, data.columns)
    return data.columns
  } catch {
    return []
  } finally {
    columnsLoading.value = ''
  }
}

// 编辑载入时先赋表再赋列，watcher 的清列动作须被抑制（否则 flush 时
// 会把 openEdit 刚写入的列清空）；列缓存在 openEdit 内显式预热
let loadingForm = false

watch(srcTable, async (key) => {
  if (loadingForm) return
  srcCols.value = []
  if (key) await ensureColumns(key)
})
watch(dstTable, async (key) => {
  if (loadingForm) return
  dstCols.value = []
  if (key) await ensureColumns(key)
})

function openAdd() {
  loadingForm = false
  formOpen.value = true
  formIsEdit.value = false
  formError.value = ''
  srcTable.value = ''
  dstTable.value = ''
  srcCols.value = []
  dstCols.value = []
  cardinality.value = 'N:1'
  editingKey.value = ''
}

function openEdit(row: DraftRow) {
  loadingForm = true
  formOpen.value = true
  formIsEdit.value = true
  formError.value = ''
  editingKey.value = row.key
  srcTable.value = `${row.fromSchema}.${row.fromTable}`
  dstTable.value = `${row.toSchema}.${row.toTable}`
  srcCols.value = [...row.fromCols]
  dstCols.value = [...row.toCols]
  cardinality.value = row.cardinality
  void ensureColumns(srcTable.value)
  void ensureColumns(dstTable.value)
  nextTick(() => {
    loadingForm = false
  })
}

function toggleCol(list: string[], column: string) {
  const at = list.indexOf(column)
  if (at >= 0) list.splice(at, 1)
  else list.push(column)
}

function splitTable(key: string): [string, string] {
  const [schema, table] = key.split('.', 2)
  return [schema ?? '', table ?? '']
}

// 左侧即 from（箭头起点），右侧即 to（箭头终点）
const resolved = computed(() => {
  const [fromSchema, fromTable] = splitTable(srcTable.value)
  const [toSchema, toTable] = splitTable(dstTable.value)
  return {
    fromSchema,
    fromTable,
    fromCols: srcCols.value,
    toSchema,
    toTable,
    toCols: dstCols.value,
  }
})

const cardinalityHint = computed(() =>
  cardinality.value === 'N:1'
    ? 'N:1：左侧是多的一侧（外键所在表），右侧是一的一侧（被引用表）'
    : '两侧对等，箭头只决定写入顺序',
)

function swapSides() {
  closeTablePicker()
  loadingForm = true
  const table = srcTable.value
  const cols = [...srcCols.value]
  srcTable.value = dstTable.value
  srcCols.value = [...dstCols.value]
  dstTable.value = table
  dstCols.value = cols
  nextTick(() => {
    loadingForm = false
  })
}

const tableHits = computed(() => {
  const keyword = tableQuery.value.trim().toLowerCase()
  if (!keyword) return tables.value
  return tables.value.filter((name) => name.toLowerCase().includes(keyword))
})

function closeTablePicker() {
  tablePicker.value = ''
}

async function toggleTablePicker(side: 'src' | 'dst') {
  if (tablePicker.value === side) {
    closeTablePicker()
    return
  }
  tablePicker.value = side
  tableQuery.value = ''
  await nextTick()
  tableQueryInput.value?.focus()
}

function onTablePickerFocusOut(event: FocusEvent) {
  const next = event.relatedTarget
  if (next instanceof Node && (event.currentTarget as HTMLElement).contains(next)) return
  closeTablePicker()
}

function pickTable(side: 'src' | 'dst', name: string) {
  if (side === 'src') srcTable.value = name
  else dstTable.value = name
  tableQuery.value = ''
  closeTablePicker()
}

const previewText = computed(() => {
  const r = resolved.value
  if (!r.fromSchema || !r.toSchema || !r.fromCols.length || !r.toCols.length) return ''
  if (r.fromCols.length !== r.toCols.length) return ''
  return `${r.fromSchema}.${r.fromTable}.${r.fromCols.length > 1 ? `[${r.fromCols.join(', ')}]` : r.fromCols[0]} → ${r.toSchema}.${r.toTable}.${r.toCols.length > 1 ? `[${r.toCols.join(', ')}]` : r.toCols[0]} (${cardinality.value})`
})

const columnMismatch = computed(() => {
  const left = srcCols.value.length
  const right = dstCols.value.length
  if (left === 0 || right === 0 || left === right) return ''
  return `两侧列数必须相同（当前起点 ${left} 列，终点 ${right} 列）。多字段关联按顺序一一对应。`
})

function submitForm() {
  const r = resolved.value
  if (!srcTable.value || !dstTable.value) {
    formError.value = '请选择起点表与终点表'
    return
  }
  if (!r.fromCols.length || !r.toCols.length) {
    formError.value = '请为两侧各选择至少一个列（第 i 个起点列对应第 i 个终点列）'
    return
  }
  if (r.fromCols.length !== r.toCols.length) {
    formError.value = `两侧列数必须一致（起点 ${r.fromCols.length} / 终点 ${r.toCols.length}）`
    return
  }
  const duplicate = draft.value.some(
    (row) =>
      row.key !== editingKey.value &&
      row.fromSchema === r.fromSchema &&
      row.fromTable === r.fromTable &&
      row.fromCols.join('\u0000') === r.fromCols.join('\u0000') &&
      row.toSchema === r.toSchema &&
      row.toTable === r.toTable &&
      row.toCols.join('\u0000') === r.toCols.join('\u0000'),
  )
  if (duplicate) {
    formError.value = '这条关系已存在'
    return
  }
  const next: DraftRow = {
    key: formIsEdit.value ? editingKey.value : `new-${++keySeq}`,
    id: null,
    fromSchema: r.fromSchema,
    fromTable: r.fromTable,
    fromCols: [...r.fromCols],
    toSchema: r.toSchema,
    toTable: r.toTable,
    toCols: [...r.toCols],
    cardinality: cardinality.value,
    method: 'manual',
    score: 1.0,
    baselineMethod: 'manual',
    baselineScore: 1.0,
    orig: null,
  }
  if (formIsEdit.value) {
    const original = draft.value.find((d) => d.key === editingKey.value)
    next.id = original?.id ?? null
    next.orig = original?.orig ?? null
    next.baselineMethod = original?.baselineMethod ?? 'manual'
    next.baselineScore = original?.baselineScore ?? 1.0
    const at = draft.value.findIndex((d) => d.key === editingKey.value)
    draft.value.splice(at, 1, next)
  } else {
    draft.value.push(next)
  }
  formOpen.value = false
}

// ---- 保存 ----

function buildPayload(): RelConfigRelationshipInput[] {
  // 未达阈值的行不在表格里，但仍在 draft 中；整表提交才能把它们原样写回。
  return draft.value.map((row) => ({
    relationship_id: row.id ?? undefined,
    from_table: { schema: row.fromSchema, table: row.fromTable },
    to_table: { schema: row.toSchema, table: row.toTable },
    from_columns: row.fromCols,
    to_columns: row.toCols,
    cardinality: row.cardinality,
  }))
}

async function save() {
  if (!canSave.value) return
  saving.value = true
  saveError.value = ''
  saveStale.value = false
  try {
    // §3.3 偏离表：三类计数是「本地列表对 GET 基线」的会话事实——必须在
    // 用服务端响应重建基线之前捕获（重建后 draft 与新基线一致，恒为 0）
    const counts = { ...diffCounts.value }
    const response = await putRelConfig(props.sourceId, props.jobId, {
      fingerprint: fingerprint.value,
      relationships: buildPayload(),
    })
    fingerprint.value = response.fingerprint
    rawBaseline.value = response.relationships
    draft.value = response.relationships.map((raw, index) => entryFromRaw(raw, index))
    savedCard.value = counts
    // 注意：不在此处 emit('saved')——父组件会立即 v-if 卸载本弹窗，
    // 成功卡片（在弹窗内部）将永远渲染不出来；改由卡片的「完成」按钮
    // emit，父组件届时关闭（dim_value 先例的成功 UI 在父组件，无此问题）
  } catch (error) {
    if (error instanceof ApiError && error.code === 'rel_regenerated') {
      saveStale.value = true
      saveError.value = 'Rel 产物已重新生成，请重新加载后再编辑'
    } else if (error instanceof ApiError && error.code === 'rel_write_failed') {
      saveError.value = `${error.message}（可直接重试）`
    } else {
      saveError.value = error instanceof ApiError ? error.message : '保存 Rel 配置失败'
    }
  } finally {
    saving.value = false
  }
}

async function reloadAfterStale() {
  saveStale.value = false
  saveError.value = ''
  await load()
}

// 「查看 rel json 文件」：前端格式化预览（GET 数据 + 本地草稿渲染，不新增端点）
const jsonPreview = computed(() => {
  const relationships = draft.value.map((row) => {
    const method = effectiveMethod(row)
    const base: Record<string, unknown> = {
      relationship_id:
        row.id ?? `rel_manual_<保存时生成#${draft.value.indexOf(row) + 1}>`,
      type: row.fromCols.length > 1 ? 'composite' : 'single_column',
      from_table: { schema: row.fromSchema, table: row.fromTable },
      to_table: { schema: row.toSchema, table: row.toTable },
      discovery_method: method,
      target_source_type: method,
      source_constraint: null,
      composite_score: effectiveScore(row),
      confidence_level: effectiveScore(row) >= 0.9 ? 'high' : 'medium',
      cardinality: row.cardinality,
    }
    if (row.fromCols.length === 1) {
      base.from_column = row.fromCols[0]
      base.to_column = row.toCols[0]
    } else {
      base.from_columns = row.fromCols
      base.to_columns = row.toCols
    }
    return base
  })
  return JSON.stringify({ database: database.value, relationships }, null, 2)
})

function methodLabel(row: DraftRow): string {
  return METHOD_LABELS[effectiveMethod(row)] ?? effectiveMethod(row)
}

function scorePercent(row: DraftRow): number {
  return Math.round(effectiveScore(row) * 100)
}

function confirmDelete() {
  if (!pendingDelete.value) return
  draft.value = draft.value.filter((d) => d.key !== pendingDelete.value!.key)
  pendingDelete.value = null
}

function close() {
  emit('close')
}

// 成功卡片「完成」：此刻才通知父组件（emit 'saved' → 父关闭弹窗）。
// 不能在 save() 成功分支同步 emit——父组件 v-if 卸载会让卡片渲染不出来。
function finishSaved() {
  emit('saved')
}
</script>

<template>
  <div class="dialog-backdrop" @click.self="close">
    <div class="rel-dialog" role="dialog" aria-modal="true">
      <header class="rel-head">
        <div>
          <span class="rel-kicker">TABLE RELATIONSHIP CONFIG</span>
          <h2>表关联关系配置</h2>
          <p class="rel-path">
            databases &gt; {{ database || '…' }} &gt; rel &gt;
            {{ database }}.relationships_global.json
          </p>
        </div>
        <button type="button" class="dialog-close" aria-label="关闭" @click="close">✕</button>
      </header>

      <p v-if="loading" class="rel-state">正在读取 Rel 产物…</p>
      <p v-else-if="fatal" class="rel-state error">{{ fatal }}</p>

      <template v-else>
        <!-- 统计标签行（只数当前阈值下可见的行） -->
        <div class="rel-chips">
          <span class="rel-chip">{{ stats.total }} 候选关系</span>
          <span class="rel-chip fk">{{ stats.fk }} 外键约束</span>
          <span class="rel-chip inf">{{ stats.inferred }} 推断</span>
          <span class="rel-chip man">{{ stats.manual }} 手动新增</span>
        </div>

        <!-- 关系表格（五列；低于 cql 阈值的非外键关系不展示） -->
        <div class="rel-table" :class="{ dense: visibleRows.length > 8 }">
          <div class="rel-row rel-head-row">
            <span>#</span><span>关联关系</span><span>发现方式</span><span>置信度</span><span>操作</span>
          </div>
          <div
            v-for="(row, index) in visibleRows"
            :key="row.key"
            class="rel-row"
          >
            <span>{{ index + 1 }}</span>
            <span class="rel-expr">
              {{ row.fromSchema }}.{{ row.fromTable }}.{{
                row.fromCols.length > 1 ? `[${row.fromCols.join(', ')}]` : row.fromCols[0]
              }}
              →
              {{ row.toSchema }}.{{ row.toTable }}.{{
                row.toCols.length > 1 ? `[${row.toCols.join(', ')}]` : row.toCols[0]
              }}
              <em>{{ row.cardinality }}</em>
            </span>
            <span class="rel-method" :class="effectiveMethod(row)">{{ methodLabel(row) }}</span>
            <span class="rel-score">
              <i :style="{ width: `${scorePercent(row)}%` }"></i>
              <b>{{ scorePercent(row) }}%</b>
            </span>
            <span class="rel-ops">
              <button type="button" @click="openEdit(row)">编辑</button>
              <button type="button" class="danger" @click="pendingDelete = row">删除</button>
            </span>
          </div>
          <p v-if="!visibleRows.length" class="rel-empty">
            没有达到当前置信度阈值的关联关系，可点下方新增
          </p>
        </div>

        <!-- 新增/修改表单（偏离：列名多选 chips；基数 {N:1,1:1,M:N}） -->
        <button
          v-if="!formOpen"
          type="button"
          class="rel-add-entry"
          @click="openAdd"
        >
          + 新增关系
        </button>
        <div v-else class="rel-form">
          <h3>{{ formIsEdit ? `编辑关系 #${visibleRows.findIndex((d) => d.key === editingKey) + 1}` : '新增关联关系（ADD RELATIONSHIP）' }}</h3>
          <div class="rel-pair">
            <div class="rel-table-picker" @focusout="onTablePickerFocusOut">
              <span>起点表</span>
              <div class="rel-table-picker-control">
                <button
                  type="button"
                  class="rel-table-picker-trigger"
                  :aria-expanded="tablePicker === 'src'"
                  aria-haspopup="listbox"
                  @click="toggleTablePicker('src')"
                >
                  <span :class="{ placeholder: !srcTable }">{{ srcTable || '选择起点表…' }}</span>
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
                </button>
                <div v-if="tablePicker === 'src'" class="rel-table-picker-panel" role="listbox">
                  <label class="rel-table-picker-search">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
                    <input
                      ref="tableQueryInput"
                      v-model="tableQuery"
                      type="text"
                      placeholder="搜索 schema.table…"
                      aria-label="搜索起点表"
                      @keydown.esc.prevent="closeTablePicker"
                    />
                  </label>
                  <p v-if="!tableHits.length" class="rel-table-picker-empty">没有匹配的表</p>
                  <ul v-else>
                    <li v-for="name in tableHits" :key="`s-${name}`">
                      <button
                        type="button"
                        role="option"
                        :aria-selected="name === srcTable"
                        :class="{ active: name === srcTable }"
                        @mousedown.prevent="pickTable('src', name)"
                      >
                        {{ name }}
                      </button>
                    </li>
                  </ul>
                </div>
              </div>
            </div>
            <span class="rel-arrow" aria-hidden="true">→</span>
            <div class="rel-table-picker" @focusout="onTablePickerFocusOut">
              <span>终点表</span>
              <div class="rel-table-picker-control">
                <button
                  type="button"
                  class="rel-table-picker-trigger"
                  :aria-expanded="tablePicker === 'dst'"
                  aria-haspopup="listbox"
                  @click="toggleTablePicker('dst')"
                >
                  <span :class="{ placeholder: !dstTable }">{{ dstTable || '选择终点表…' }}</span>
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
                </button>
                <div v-if="tablePicker === 'dst'" class="rel-table-picker-panel" role="listbox">
                  <label class="rel-table-picker-search">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
                    <input
                      ref="tableQueryInput"
                      v-model="tableQuery"
                      type="text"
                      placeholder="搜索 schema.table…"
                      aria-label="搜索终点表"
                      @keydown.esc.prevent="closeTablePicker"
                    />
                  </label>
                  <p v-if="!tableHits.length" class="rel-table-picker-empty">没有匹配的表</p>
                  <ul v-else>
                    <li v-for="name in tableHits" :key="`d-${name}`">
                      <button
                        type="button"
                        role="option"
                        :aria-selected="name === dstTable"
                        :class="{ active: name === dstTable }"
                        @mousedown.prevent="pickTable('dst', name)"
                      >
                        {{ name }}
                      </button>
                    </li>
                  </ul>
                </div>
              </div>
            </div>
            <div>
              <span class="rel-cols-title">起点列（按点击顺序配对）</span>
              <div class="rel-chips-select">
                <button
                  v-for="col in columnsCache.get(srcTable) || []"
                  :key="`sc-${col}`"
                  type="button"
                  :class="{ on: srcCols.includes(col) }"
                  @click="toggleCol(srcCols, col)"
                >
                  {{ col }}
                </button>
                <small v-if="columnsLoading === srcTable">读取列中…</small>
                <small v-else-if="srcTable && !(columnsCache.get(srcTable) || []).length">
                  该表无可用列
                </small>
              </div>
            </div>
            <button type="button" class="rel-swap" @click="swapSides">互换两侧</button>
            <div>
              <span class="rel-cols-title">终点列（第 i 个对应第 i 个）</span>
              <div class="rel-chips-select">
                <button
                  v-for="col in columnsCache.get(dstTable) || []"
                  :key="`dc-${col}`"
                  type="button"
                  :class="{ on: dstCols.includes(col) }"
                  @click="toggleCol(dstCols, col)"
                >
                  {{ col }}
                </button>
                <small v-if="columnsLoading === dstTable">读取列中…</small>
                <small v-else-if="dstTable && !(columnsCache.get(dstTable) || []).length">
                  该表无可用列
                </small>
              </div>
            </div>
          </div>
          <div class="rel-form-row">
            <div class="rel-seg">
              <span>基数</span>
              <button
                v-for="c in CARDINALITIES"
                :key="c"
                type="button"
                :class="{ on: cardinality === c }"
                @click="cardinality = c"
              >
                {{ c }}
              </button>
              <small class="rel-card-hint">{{ cardinalityHint }}</small>
            </div>
          </div>
          <p v-if="columnMismatch" class="rel-form-error" role="status">{{ columnMismatch }}</p>
          <p v-else-if="previewText" class="rel-preview">
            {{ formIsEdit ? '修改后预览' : '新增关系预览' }} · 落库形态：
            <b>{{ previewText }}</b>
            <template v-if="!formIsEdit">（标记为 manual(手动新增)）</template>
          </p>
          <p v-if="formError" class="rel-form-error">{{ formError }}</p>
          <div class="rel-form-actions">
            <button type="button" class="button" @click="formOpen = false">取消</button>
            <button
              type="button"
              class="button button-primary"
              :disabled="Boolean(columnMismatch)"
              @click="submitForm"
            >
              {{ formIsEdit ? '保存修改' : '+ 保存关系' }}
            </button>
          </div>
        </div>

        <p v-if="saveError" class="rel-state error" role="alert">
          {{ saveError }}
          <button v-if="saveStale" type="button" class="rel-inline-link" @click="reloadAfterStale">
            重新加载
          </button>
        </p>

        <footer class="rel-foot">
          <p v-if="formOpen" class="rel-foot-warn">
            请先保存或取消当前关系编辑，再执行写回
          </p>
          <p v-else>保存后写回 rel JSON 与 MD；CQL 生成与 Neo4j 加载在发布 rel 时执行</p>
          <div>
            <button type="button" class="button" @click="close">取消</button>
            <button
              type="button"
              class="button button-primary"
              :disabled="!canSave"
              :title="!hasEdits && !formOpen ? '没有修改，无需保存' : undefined"
              @click="save"
            >
              {{ saving ? '保存中…' : '保存（写回 JSON 与 MD）' }}
            </button>
          </div>
        </footer>

        <!-- 删除确认（原型 rel-3） -->
        <div v-if="pendingDelete" class="rel-sub-backdrop" @click.self="pendingDelete = null">
          <div class="rel-confirm" role="alertdialog" aria-modal="true">
            <p>删除这条关联关系？</p>
            <small>确认后该关系将从 JSON 与 MD 中移除，保存后生效</small>
            <div class="dialog-actions">
              <button type="button" class="button" @click="pendingDelete = null">取消</button>
              <button type="button" class="button button-primary" @click="confirmDelete">
                确认删除
              </button>
            </div>
          </div>
        </div>

        <!-- 保存成功卡片（原型 rel-5） -->
        <div v-if="savedCard" class="rel-sub-backdrop">
          <div class="rel-saved" role="dialog" aria-modal="true">
            <span class="rel-saved-mark" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path d="M5 12.5 9.5 17 19 7" /></svg>
            </span>
            <h2>已写回 rel/{{ database }}.relationships_global.json</h2>
            <p>
              {{ savedCard.modified }} 条修改 · {{ savedCard.removed }} 条删除 ·
              {{ savedCard.added }} 条新增；发布 rel 时将使用最新关系内容
            </p>
            <button type="button" class="rel-inline-link" @click="jsonPreviewOpen = !jsonPreviewOpen">
              {{ jsonPreviewOpen ? '收起 rel json 预览' : '查看 rel json 文件' }}
            </button>
            <pre v-if="jsonPreviewOpen" class="rel-json">{{ jsonPreview }}</pre>
            <button type="button" class="button button-primary" @click="finishSaved">完成</button>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.rel-dialog {
  width: min(1120px, calc(100vw - 48px));
  min-height: min(860px, calc(100vh - 48px));
  max-height: calc(100vh - 48px);
  overflow: auto;
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 18px;
  padding: 24px 26px 18px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.rel-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}
.rel-kicker {
  color: #2e7cd6;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 3px;
}
.rel-head h2 {
  margin: 6px 0 4px;
  font-size: 20px;
  font-weight: 700;
  color: #17293f;
}
.rel-path {
  margin: 0;
  color: #66788f;
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 11px;
}
.rel-state {
  margin: 0;
  color: #66788f;
  font-size: 13px;
}
.rel-state.error {
  color: #c64f4f;
}
.rel-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.rel-chip {
  background: #eef3f9;
  border: 1px solid #d9e3ef;
  border-radius: 14px;
  padding: 5px 12px;
  font-size: 11px;
  font-weight: 700;
  color: #17293f;
}
.rel-chip.fk {
  background: #ddf0eb;
  color: #0e9f8a;
}
.rel-chip.inf {
  background: #e3eefb;
  color: #2e7cd6;
}
.rel-chip.man {
  background: #fdf3e3;
  color: #8a5a16;
}
.rel-table {
  border: 1px solid #d9e3ef;
  border-radius: 12px;
  overflow: hidden;
}
.rel-row {
  display: grid;
  grid-template-columns: 36px minmax(0, 2.4fr) 108px 120px 116px;
  gap: 10px;
  align-items: center;
  padding: 9px 12px;
  border-bottom: 1px solid #e6edf6;
  font-size: 12px;
}
.rel-row:last-child {
  border-bottom: 0;
}
.rel-head-row {
  background: #eef3f9;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1px;
  color: #66788f;
}
.rel-expr {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 11px;
  word-break: break-all;
}
.rel-expr em {
  font-style: normal;
  background: #e3eefb;
  color: #2e7cd6;
  border-radius: 6px;
  padding: 1px 6px;
  margin-left: 6px;
  font-size: 10px;
  font-weight: 700;
}
.rel-method {
  font-size: 11px;
  font-weight: 700;
  color: #425671;
}
.rel-method.foreign_key_constraint {
  color: #0e9f8a;
}
.rel-method.manual {
  color: #8a5a16;
}
.rel-score {
  display: flex;
  align-items: center;
  gap: 6px;
}
.rel-score i {
  height: 5px;
  border-radius: 4px;
  background: #2e7cd6;
  min-width: 4px;
}
.rel-score b {
  font-size: 10px;
  color: #66788f;
}
.rel-ops {
  display: flex;
  gap: 6px;
  justify-content: flex-end;
}
.rel-ops button {
  border: 1px solid #c5d7ee;
  background: #fff;
  color: #2e7cd6;
  border-radius: 8px;
  padding: 3px 9px;
  font-size: 10px;
  font-weight: 700;
}
.rel-ops button.danger {
  border-color: #efcaca;
  color: #c64f4f;
}
.rel-empty {
  margin: 0;
  padding: 14px;
  color: #66788f;
  font-size: 12px;
  text-align: center;
}
.rel-add-entry {
  border: 1.5px dashed #b7c9e2;
  border-radius: 10px;
  background: #f7fafd;
  color: #2e7cd6;
  padding: 10px;
  font-size: 12px;
  font-weight: 700;
}
.rel-form {
  border: 1px solid #c5d7ee;
  border-radius: 12px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: #fbfdff;
}
.rel-form h3 {
  margin: 0;
  font-size: 13px;
  color: #17293f;
}
.rel-pair {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  gap: 12px 10px;
  align-items: start;
}
.rel-arrow {
  justify-self: center;
  align-self: end;
  margin-bottom: 8px;
  color: #2e7cd6;
  font-size: 18px;
  font-weight: 700;
  line-height: 1;
}
.rel-swap {
  align-self: center;
  margin-top: 18px;
  border: 1px solid #c5d7ee;
  background: #fff;
  color: #2e7cd6;
  border-radius: 8px;
  padding: 6px 8px;
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}
.rel-form label {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 11px;
  font-weight: 700;
  color: #425671;
}
.rel-table-picker {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 11px;
  font-weight: 700;
  color: #425671;
}
.rel-table-picker-control {
  position: relative;
}
.rel-table-picker-trigger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  border: 1px solid #d2e0f2;
  border-radius: 8px;
  background: #fff;
  padding: 8px 10px;
  font-size: 12px;
  font-weight: 700;
  color: #17293f;
  cursor: pointer;
  text-align: left;
}
.rel-table-picker-trigger .placeholder {
  font-weight: 400;
  color: #8a99ac;
}
.rel-table-picker-trigger svg,
.rel-table-picker-search svg {
  width: 14px;
  height: 14px;
  flex: 0 0 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.rel-table-picker-trigger svg {
  color: #66788f;
}
.rel-table-picker-panel {
  position: absolute;
  z-index: 30;
  top: calc(100% + 6px);
  left: 0;
  right: 0;
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 12px;
  box-shadow: 0 8px 24px rgba(23, 41, 63, 0.1);
  padding: 8px;
}
.rel-table-picker-search {
  display: flex;
  align-items: center;
  gap: 6px;
  border: 1px solid #d9e3ef;
  border-radius: 8px;
  padding: 6px 8px;
  color: #66788f;
}
.rel-table-picker-search input {
  width: 100%;
  min-width: 0;
  border: 0;
  padding: 0;
  outline: none;
  background: transparent;
  font-size: 12px;
  font-weight: 400;
  color: #17293f;
}
.rel-table-picker-panel ul {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  max-height: 220px;
  overflow: auto;
}
.rel-table-picker-panel li button {
  width: 100%;
  text-align: left;
  border: 0;
  background: transparent;
  border-radius: 8px;
  padding: 7px 10px;
  font-size: 12px;
  font-weight: 700;
  color: #17293f;
  cursor: pointer;
}
.rel-table-picker-panel li button.active,
.rel-table-picker-panel li button:hover {
  background: #e3eefb;
  color: #2e7cd6;
}
.rel-table-picker-empty {
  margin: 8px 0 0;
  padding: 8px 10px;
  color: #66788f;
  font-size: 12px;
  font-weight: 400;
}
.rel-cols-title {
  display: block;
  font-size: 11px;
  font-weight: 700;
  color: #425671;
  margin-bottom: 6px;
}
.rel-chips-select {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-height: 34px;
  border: 1px solid #d9e3ef;
  border-radius: 8px;
  padding: 6px;
  background: #fff;
}
.rel-chips-select button {
  border: 1px solid #c5d7ee;
  background: #fff;
  color: #425671;
  border-radius: 12px;
  padding: 3px 10px;
  font-size: 10px;
  font-family: 'JetBrains Mono', ui-monospace, monospace;
}
.rel-chips-select button.on {
  background: #2e7cd6;
  border-color: #2e7cd6;
  color: #fff;
}
.rel-chips-select small {
  color: #8a99ac;
  font-size: 10px;
  align-self: center;
}
.rel-form-row {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
}
.rel-card-hint {
  color: #8a99ac;
  font-size: 11px;
  font-weight: 500;
  margin-left: 6px;
}
.rel-seg {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 11px;
  font-weight: 700;
  color: #425671;
}
.rel-seg > span {
  margin-right: 2px;
}
.rel-seg button {
  border: 1px solid #c5d7ee;
  background: #fff;
  border-radius: 14px;
  padding: 5px 12px;
  font-size: 11px;
  font-weight: 700;
  color: #425671;
}
.rel-seg button.on {
  background: #2e7cd6;
  border-color: #2e7cd6;
  color: #fff;
}
.rel-preview {
  margin: 0;
  font-size: 12px;
  color: #425671;
}
.rel-preview b {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-weight: 700;
  color: #17293f;
}
.rel-form-error {
  margin: 0;
  color: #c64f4f;
  font-size: 12px;
  font-weight: 600;
}
.rel-form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
.rel-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 14px;
  border-top: 1px solid #e1e9f4;
  padding-top: 12px;
}
.rel-foot p {
  margin: 0;
  color: #66788f;
  font-size: 11px;
}
.rel-foot .rel-foot-warn {
  color: #b07c00;
  font-weight: 700;
}
.rel-foot > div {
  display: flex;
  gap: 10px;
}
.rel-inline-link {
  border: 0;
  background: transparent;
  color: #2e7cd6;
  font-size: 12px;
  font-weight: 700;
  text-decoration: underline;
  padding: 0 4px;
}
.rel-sub-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(22, 38, 61, 0.35);
  display: grid;
  place-items: center;
  border-radius: 18px;
}
.rel-dialog {
  position: relative;
}
.rel-confirm {
  width: min(360px, calc(100vw - 80px));
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 14px;
  padding: 18px;
  text-align: center;
}
.rel-confirm p {
  margin: 0 0 6px;
  font-size: 14px;
  font-weight: 700;
  color: #17293f;
}
.rel-confirm small {
  color: #66788f;
  font-size: 11px;
}
.rel-confirm .dialog-actions {
  margin-top: 14px;
  justify-content: center;
}
.rel-saved {
  width: min(480px, calc(100vw - 80px));
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 16px;
  padding: 22px 20px;
  text-align: center;
  display: flex;
  flex-direction: column;
  gap: 10px;
  align-items: center;
}
.rel-saved-mark {
  width: 44px;
  height: 44px;
  border-radius: 50%;
  background: #e7f6ef;
  color: #1aae78;
  display: grid;
  place-items: center;
}
.rel-saved-mark svg {
  width: 20px;
  height: 20px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.rel-saved h2 {
  margin: 0;
  font-size: 15px;
  color: #17293f;
  word-break: break-all;
}
.rel-saved p {
  margin: 0;
  color: #66788f;
  font-size: 12px;
}
.rel-json {
  width: 100%;
  max-height: 220px;
  overflow: auto;
  text-align: left;
  background: #eef3f9;
  border-radius: 10px;
  padding: 10px 12px;
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 10px;
  color: #17293f;
}
@media (max-width: 800px) {
  .rel-pair {
    grid-template-columns: 1fr;
  }
  .rel-arrow,
  .rel-swap {
    justify-self: center;
    margin: 0;
  }
  .rel-row {
    grid-template-columns: 28px minmax(0, 1fr) 96px 90px;
  }
  .rel-row > :nth-child(5) {
    grid-column: 2;
  }
}
</style>
