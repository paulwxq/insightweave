<script setup lang="ts">
/**
 * 一条 SQL 证据：预览表格、查看全部、展开 SQL、导出、简单图表。
 * 结果按 result_ref 分页读取，查询响应本身不带行数据。
 */
import { computed, onMounted, ref } from 'vue'
import { getResult } from '../api/client'
import type { CellV2, ResultPageData } from '../api/types'
import { formatElapsed } from '../format'
import { cellDisplay } from '../utils/cellDisplay'
import ResultTable from './ResultTable.vue'
import SqlBlock from './SqlBlock.vue'

const props = defineProps<{
  sessionId: string
  sql: string | null
  resultRef: string | null
  elapsedMs: number | null
  sourceTruncated: boolean
}>()

const PREVIEW = 10
const PAGE_SIZE = 100
const MAX_ROWS = 200

const page = ref<ResultPageData | null>(null)
const extraRows = ref<CellV2[][]>([])
const loading = ref(false)
const error = ref('')
const showAll = ref(false)
const sqlOpen = ref(false)
const chartOpen = ref(false)
const exporting = ref(false)

const loadedRows = computed(() => {
  if (!page.value) return []
  return [...page.value.rows, ...extraRows.value]
})

const visibleRows = computed(() =>
  showAll.value ? loadedRows.value : loadedRows.value.slice(0, PREVIEW),
)

const totalCount = computed(() => page.value?.saved_row_count ?? loadedRows.value.length)

const shownCount = computed(() => visibleRows.value.length)

const canReveal = computed(() => {
  if (!page.value) return false
  return loadedRows.value.length > PREVIEW || page.value.has_more || extraRows.value.length > 0
})

const elapsedText = computed(() => formatElapsed(props.elapsedMs))

const chartSeries = computed(() => {
  const data = page.value
  if (!data || visibleRows.value.length === 0) return null
  const numericIndex = data.columns.findIndex((_, index) =>
    visibleRows.value.some((row) => {
      const cell = row[index]
      return cell && (cell.kind === 'integer' || cell.kind === 'decimal' || cell.kind === 'float')
    }),
  )
  if (numericIndex < 0) return null
  const labelIndex = data.columns.findIndex(
    (_, index) => index !== numericIndex,
  )
  const points = visibleRows.value.slice(0, 8).map((row, index) => {
    const raw = row[numericIndex]?.value
    const value = typeof raw === 'number' ? raw : Number(raw)
    const labelCell = labelIndex >= 0 ? row[labelIndex] : undefined
    return {
      label: labelCell ? cellDisplay(labelCell).text : String(index + 1),
      value: Number.isFinite(value) ? value : 0,
    }
  })
  const max = Math.max(...points.map((item) => item.value), 1)
  return {
    valueName: data.columns[numericIndex]?.name ?? '数值',
    points: points.map((item) => ({ ...item, width: `${Math.max(4, (item.value / max) * 100)}%` })),
  }
})

async function load(offset: number, append: boolean) {
  if (!props.resultRef || props.sessionId === '') return
  loading.value = true
  error.value = ''
  try {
    const next = await getResult(props.sessionId, props.resultRef, offset, PAGE_SIZE)
    if (append && page.value) {
      extraRows.value = [...extraRows.value, ...next.rows]
      page.value = { ...page.value, has_more: next.has_more, next_offset: next.next_offset }
    } else {
      page.value = next
      extraRows.value = []
    }
  } catch (err) {
    error.value = err instanceof Error && err.message ? err.message : '结果读取失败，请稍后重试。'
  } finally {
    loading.value = false
  }
}

async function revealAll() {
  showAll.value = true
  let guard = 0
  while (
    page.value?.has_more &&
    page.value.next_offset !== null &&
    loadedRows.value.length < MAX_ROWS &&
    guard < 8
  ) {
    const offset = page.value.next_offset
    await load(offset, true)
    guard += 1
  }
}

function csvCell(value: string): string {
  if (/[",\n]/.test(value)) return `"${value.replace(/"/g, '""')}"`
  return value
}

async function exportCsv() {
  if (!page.value || exporting.value) return
  exporting.value = true
  try {
    if (!showAll.value) await revealAll()
    const columns = page.value.columns
    const lines = [
      columns.map((column) => csvCell(column.name)).join(','),
      ...loadedRows.value.map((row) =>
        row.map((cell) => csvCell(cellDisplay(cell).text)).join(','),
      ),
    ]
    const blob = new Blob([`\uFEFF${lines.join('\n')}`], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'query-result.csv'
    link.click()
    URL.revokeObjectURL(url)
  } finally {
    exporting.value = false
  }
}

onMounted(() => {
  if (props.resultRef) void load(0, false)
})
</script>

<template>
  <div class="evidence">
    <p v-if="loading && !page" class="hint">正在读取结果…</p>
    <p v-else-if="error" class="hint warn">{{ error }}</p>
    <template v-else-if="page">
      <p v-if="page.rows.length === 0" class="hint">查询已执行，没有返回数据行。</p>
      <ResultTable v-else :columns="page.columns" :rows="visibleRows" />

      <div class="stats">
        <span>
          已显示 {{ shownCount }} / 共 {{ totalCount }} 条
          <template v-if="elapsedText"> · {{ elapsedText }}</template>
          <template v-if="sourceTruncated || page.source_truncated"> · 结果已截断</template>
        </span>
        <button v-if="canReveal && !showAll" type="button" class="link" @click="revealAll">
          查看全部
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
        </button>
      </div>
    </template>

    <div v-if="sql || resultRef" class="actions">
      <button v-if="sql" type="button" class="sql-toggle" @click="sqlOpen = !sqlOpen">
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path v-if="sqlOpen" d="m4 6 4 4 4-4" />
          <path v-else d="m6 4 4 4-4 4" />
        </svg>
        {{ sqlOpen ? '收起 SQL' : '查看 SQL' }}
      </button>
      <span v-else></span>
      <div v-if="resultRef" class="action-right">
        <button type="button" class="ghost" :disabled="!page" @click="chartOpen = !chartOpen">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 13V3M2 13h12M5 10V7M8 10V5M11 10V8" /></svg>
          生成图表
        </button>
        <button type="button" class="ghost" :disabled="!page || exporting" @click="exportCsv">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2v8M5 7l3 3 3-3M3 13h10" /></svg>
          {{ exporting ? '导出中…' : '导出' }}
        </button>
      </div>
    </div>

    <SqlBlock v-if="sqlOpen && sql" :sql="sql" />

    <div v-if="chartOpen" class="chart">
      <template v-if="chartSeries">
        <p>{{ chartSeries.valueName }}</p>
        <div v-for="point in chartSeries.points" :key="point.label" class="bar-row">
          <span>{{ point.label }}</span>
          <i :style="{ width: point.width }"></i>
          <b>{{ point.value }}</b>
        </div>
      </template>
      <p v-else class="hint">当前结果没有可绘制的数值列。</p>
    </div>
  </div>
</template>

<style scoped>
.evidence {
  display: grid;
  gap: 12px;
}

.hint {
  margin: 0;
  color: #66788f;
  font-size: 12px;
}

.hint.warn {
  color: #9a6b32;
}

.stats {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  color: #66788f;
  font-size: 12px;
}

.link,
.sql-toggle,
.ghost {
  border: 0;
  background: transparent;
  color: #2e7cd6;
  font-size: 12px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0;
}

.ghost {
  border: 1px solid #d9e3ef;
  background: #fff;
  color: #41597a;
  border-radius: 8px;
  padding: 6px 10px;
  font-weight: 600;
}

.ghost:disabled,
.link:disabled {
  opacity: 0.5;
}

.link svg,
.sql-toggle svg,
.ghost svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-top: 4px;
  border-top: 1px solid #e7eef6;
}

.action-right {
  display: flex;
  gap: 8px;
}

.chart {
  display: grid;
  gap: 8px;
  background: #f7fafc;
  border-radius: 10px;
  padding: 12px;
}

.chart p {
  margin: 0;
  color: #66788f;
  font-size: 12px;
}

.bar-row {
  display: grid;
  grid-template-columns: 88px 1fr auto;
  gap: 8px;
  align-items: center;
  font-size: 12px;
  color: #17293f;
}

.bar-row i {
  display: block;
  height: 8px;
  border-radius: 99px;
  background: #2e7cd6;
}

.bar-row b {
  font-weight: 600;
  color: #41597a;
}
</style>
