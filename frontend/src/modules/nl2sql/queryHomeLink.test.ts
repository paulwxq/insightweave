import assert from 'node:assert/strict'
import { test } from 'node:test'

import { queryHomeTarget, sessionBackTarget } from './queryHomeLink.ts'

test('首页筛选按来源、标题和页码带回', () => {
  assert.equal(queryHomeTarget({}), '/query')
  assert.equal(
    queryHomeTarget({ source_id: 'db_src_1', q: ' order ', page: '2' }),
    '/query?source_id=db_src_1&q=order&page=2',
  )
  assert.equal(queryHomeTarget({ page: '1' }), '/query')
  assert.equal(queryHomeTarget({ source_id: 'other', q: '测'.repeat(101) }), '/query')
})

test('会话返回只接受问数首页，丢掉其他来源', () => {
  assert.equal(sessionBackTarget(undefined), '/query')
  assert.equal(sessionBackTarget('/query?q=Order&page=3'), '/query?q=Order&page=3')
  assert.equal(sessionBackTarget('//evil/query'), '/query')
  assert.equal(sessionBackTarget('https://evil.com/query?q=order'), '/query')
  assert.equal(sessionBackTarget('/query/new'), '/query')
  assert.equal(sessionBackTarget('/query/s_abc'), '/query')
  assert.equal(
    sessionBackTarget('/query?from=/data-sources&q=order&source_id=nope'),
    '/query?q=order',
  )
})
