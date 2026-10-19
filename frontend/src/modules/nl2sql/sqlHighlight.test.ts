import assert from 'node:assert/strict'
import { test } from 'node:test'

import { formatClock, formatElapsed, formatRelativeTime, formatTurnDivider } from './format.ts'
import { highlightSql } from './sqlHighlight.ts'

test('SQL 关键字、AS 和字符串分成不同记号', () => {
  const tokens = highlightSql("SELECT name AS title FROM t WHERE id = 'a'")
  const kinds = Object.fromEntries(tokens.filter((item) => item.text.trim()).map((item) => [item.text, item.kind]))
  assert.equal(kinds.SELECT, 'keyword')
  assert.equal(kinds.AS, 'as')
  assert.equal(kinds.FROM, 'keyword')
  assert.equal(kinds["'a'"], 'string')
  assert.equal(kinds.name, 'text')
})

test('耗时按秒展示', () => {
  assert.equal(formatElapsed(380), '耗时 0.38 秒')
  assert.equal(formatElapsed(null), '')
})

test('同一天显示今天和相对时间', () => {
  const now = new Date('2026-10-18T06:32:00Z')
  const earlier = '2026-10-18T06:20:00Z'
  assert.equal(formatClock(earlier), '14:20')
  assert.equal(formatTurnDivider(earlier, now), '今天 14:20')
  assert.equal(formatRelativeTime(earlier, now), '12 分钟前')
})

test('昨天与多天前', () => {
  const now = new Date('2026-10-18T06:32:00Z')
  assert.equal(formatTurnDivider('2026-10-17T10:07:00Z', now), '昨天 18:07')
  assert.equal(formatRelativeTime('2026-10-15T06:32:00Z', now), '3 天前')
})
