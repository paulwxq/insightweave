import assert from 'node:assert/strict'
import { test } from 'node:test'

import { cellDisplay } from './cellDisplay.ts'

test('标量 kind 按各自规则展示', () => {
  assert.equal(cellDisplay({ kind: 'integer', value: '1152921504606846976' }).text, '1152921504606846976')
  assert.equal(cellDisplay({ kind: 'decimal', value: '1.50' }).text, '1.50')
  assert.equal(cellDisplay({ kind: 'boolean', value: true }).text, 'true')
  assert.equal(cellDisplay({ kind: 'null', value: null }).text, 'NULL')
  assert.equal(cellDisplay({ kind: 'float', value: 'NaN' }).text, 'NaN')
  assert.equal(
    cellDisplay({ kind: 'json', value: '{"$dec": [1, "a"]}' }).text,
    '{"$dec": [1, "a"]}',
  )
  assert.equal(cellDisplay({ kind: 'binary', value: { length: 3, content_saved: false } }).text, '二进制 3 字节')
})

test('数组元素按 cell kind 递归格式化，不再是 [object Object]', () => {
  const cell = {
    kind: 'array',
    value: [
      { kind: 'integer', value: '1' },
      { kind: 'decimal', value: '2.5' },
      { kind: 'null', value: null },
      { kind: 'text', value: 'a' },
    ],
  }
  assert.equal(cellDisplay(cell).text, '{1, 2.5, NULL, a}')
})

test('嵌套数组与 NULL 保留层级', () => {
  const cell = {
    kind: 'array',
    value: [
      { kind: 'array', value: [{ kind: 'integer', value: '1' }, { kind: 'integer', value: '2' }] },
      { kind: 'null', value: null },
      { kind: 'text', value: 'x' },
    ],
  }
  assert.equal(cellDisplay(cell).text, '{{1, 2}, NULL, x}')
})

test('空数组与 json 元素', () => {
  assert.equal(cellDisplay({ kind: 'array', value: [] }).text, '{}')
  const jsonElems = {
    kind: 'array',
    value: [
      { kind: 'json', value: '{"k": 1}' },
      { kind: 'boolean', value: false },
    ],
  }
  assert.equal(cellDisplay(jsonElems).text, '{{"k": 1}, false}')
})
