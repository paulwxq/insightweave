import assert from 'node:assert/strict'
import { test } from 'node:test'

import { summarizeResultPage } from './resultSummary.ts'

test('文件分支：总数用原保存行数，不显示快照提示', () => {
  const summary = summarizeResultPage({
    data_from_snapshot: false,
    saved_row_count: 1000,
    snapshot_rows_available: null,
  })
  assert.equal(summary.totalCount, 1000)
  assert.equal(summary.snapshotNotice, '')
})

test('快照分支：总数用快照可用行数，并提示原结果行数与导出范围', () => {
  const summary = summarizeResultPage({
    data_from_snapshot: true,
    saved_row_count: 1000,
    snapshot_rows_available: 200,
  })
  assert.equal(summary.totalCount, 200)
  assert.match(summary.snapshotNotice, /完整结果文件不可用/)
  assert.match(summary.snapshotNotice, /快照 200 行 \/ 原结果已保存 1000 行/)
  assert.match(summary.snapshotNotice, /导出仅包含快照行/)
})
