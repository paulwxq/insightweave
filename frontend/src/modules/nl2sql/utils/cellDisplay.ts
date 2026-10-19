/**
 * v2 cell → 展示文本（doc 2 §4.5 单元格编码表）。
 *
 * - integer/decimal 保持字符串原样（>2^53 安全），json 以文本显示不重算，
 *   binary 显示长度不猜测内容，NULL 弱化；
 * - array 元素为同规则 cell 信封，递归格式化（含嵌套数组与 NULL），
 *   按 PostgreSQL 数组字面量风格以花括号呈现层级。
 */
import type { BinaryCellValue, CellV2 } from '../api/types'

export interface CellDisplay {
  text: string
  cls: string
  title?: string
}

function isCellEnvelope(value: unknown): value is CellV2 {
  return (
    value !== null &&
    typeof value === 'object' &&
    typeof (value as Record<string, unknown>).kind === 'string' &&
    'value' in (value as Record<string, unknown>)
  )
}

export function cellDisplay(cell: CellV2): CellDisplay {
  if (cell.value === null) return { text: 'NULL', cls: 'nl2sql-cell-null' }
  switch (cell.kind) {
    case 'boolean':
      return { text: cell.value === true ? 'true' : 'false', cls: '' }
    case 'integer':
    case 'decimal':
      // 服务端保证为字符串；原样展示，不经 Number 重算
      return { text: String(cell.value), cls: 'nl2sql-cell-num' }
    case 'float': {
      const value = cell.value
      if (typeof value === 'number' && Number.isNaN(value)) {
        return { text: 'NaN', cls: 'nl2sql-cell-num' }
      }
      if (typeof value === 'number' && !Number.isFinite(value)) {
        return { text: value > 0 ? 'Infinity' : '-Infinity', cls: 'nl2sql-cell-num' }
      }
      return { text: String(value), cls: 'nl2sql-cell-num' }
    }
    case 'json':
      // JSON 文本字符串：以文本显示，不 JSON.parse 重算数值
      return { text: String(cell.value), cls: 'nl2sql-cell-json' }
    case 'binary': {
      const binary = cell.value as BinaryCellValue
      const length = typeof binary?.length === 'number' ? binary.length : 0
      return { text: `二进制 ${length} 字节`, cls: 'nl2sql-cell-null' }
    }
    case 'array': {
      if (Array.isArray(cell.value)) {
        const inner = cell.value.map((item) => elementText(item)).join(', ')
        return { text: `{${inner}}`, cls: '' }
      }
      return { text: String(cell.value), cls: '' }
    }
    case 'interval': {
      // 微秒字符串；原样展示避免精度与语义歧义
      return { text: String(cell.value), cls: 'nl2sql-cell-num', title: 'interval（微秒）' }
    }
    case 'uuid':
      return { text: String(cell.value), cls: 'nl2sql-cell-mono' }
    case 'date':
    case 'datetime':
    case 'time':
    case 'text':
    default:
      return { text: String(cell.value), cls: '' }
  }
}

/** 数组元素文本：元素是 cell 信封则按其 kind 递归格式化。 */
function elementText(item: unknown): string {
  if (isCellEnvelope(item)) return cellDisplay(item).text
  return String(item)
}
