<script setup lang="ts">
/**
 * 结果表格。行数据由调用方决定展示多少行；单元格按 v2 kind 显示。
 */
import type { CellV2, ResultColumn } from '../api/types'
import { cellDisplay } from '../utils/cellDisplay'

defineProps<{
  columns: ResultColumn[]
  rows: CellV2[][]
}>()

const cellText = cellDisplay
</script>

<template>
  <div class="result-scroll">
    <table class="result-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column.name" :title="column.pg_type">
            {{ column.name }}
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, rowIndex) in rows" :key="rowIndex">
          <td
            v-for="(cell, cellIndex) in row"
            :key="cellIndex"
            :class="cellText(cell).cls"
            :title="cellText(cell).title"
          >
            {{ cellText(cell).text }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.result-scroll {
  overflow: auto;
  border-radius: 10px;
  max-height: 360px;
}

.result-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  background: #fff;
}

.result-table th,
.result-table td {
  padding: 11px 14px;
  text-align: left;
  border-bottom: 1px solid #e7eef6;
  white-space: nowrap;
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  color: #17293f;
}

.result-table th {
  background: #f3f6fa;
  color: #66788f;
  font-size: 12px;
  font-weight: 500;
}

.result-table tbody tr:last-child td {
  border-bottom: 0;
}

:deep(.nl2sql-cell-num) {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

:deep(.nl2sql-cell-null) {
  color: #9aaaaf;
  font-style: italic;
}

:deep(.nl2sql-cell-mono),
:deep(.nl2sql-cell-json) {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 12px;
}
</style>
