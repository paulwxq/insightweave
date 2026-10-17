import assert from 'node:assert/strict'
import { test } from 'node:test'

import { shownAtCqlThreshold } from './relDisplayFilter.ts'

test('外键不看分数', () => {
  assert.equal(shownAtCqlThreshold('foreign_key_constraint', 0.5, 0.9), true)
  assert.equal(shownAtCqlThreshold('foreign_key_constraint', null, 0.9), true)
})

test('非外键按大于等于阈值过滤', () => {
  assert.equal(shownAtCqlThreshold('physical_key_matching', 0.85, 0.9), false)
  assert.equal(shownAtCqlThreshold('physical_key_matching', 0.9, 0.9), true)
  assert.equal(shownAtCqlThreshold('llm_inferred', 0.99, 0.9), true)
  assert.equal(shownAtCqlThreshold('manual', null, 0.9), false)
})
