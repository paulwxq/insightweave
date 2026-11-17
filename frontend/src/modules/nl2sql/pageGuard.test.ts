import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createPageGuard } from './pageGuard.ts'

test('离开后再进入两次，旧实例不能通过新页面的归属检查', () => {
  const guard = createPageGuard()
  const first = guard.openInstance()
  first.release()
  const second = guard.openInstance()
  second.release()
  const third = guard.openInstance()

  assert.equal(first.stillHere(), false)
  assert.equal(second.stillHere(), false)
  assert.equal(third.stillHere(), true)

  let pushed = false
  const lateCreate = Promise.resolve().then(() => {
    if (second.stillHere()) pushed = true
  })
  return lateCreate.then(() => {
    assert.equal(pushed, false)
    assert.equal(third.stillHere(), true)
  })
})

test('更换新建流程后，旧创建的成功、失败和收尾都不再属于当前页', () => {
  const guard = createPageGuard()
  const page = guard.openInstance()
  const started = guard.createStamp()
  guard.invalidateCreate()
  const current = () => page.stillHere() && started === guard.createStamp()

  assert.equal(current(), false)
  let notice = ''
  let creating = true
  const finished = Promise.reject(new Error('创建失败')).then(
    () => {
      if (current()) notice = '不应写入'
    },
    () => {
      if (current()) notice = '失败写入了新页面'
    },
  )
  return finished.finally(() => {
    if (current()) creating = false
    assert.equal(notice, '')
    assert.equal(creating, true)
    page.release()
    assert.equal(page.stillHere(), false)
  })
})

test('创建代只增不减：多次作废后旧快照永久失配，不与新流程碰撞', () => {
  const guard = createPageGuard()
  const page = guard.openInstance()
  const stamps: number[] = [guard.createStamp()]
  guard.invalidateCreate()
  stamps.push(guard.createStamp())
  guard.invalidateCreate()
  stamps.push(guard.createStamp())

  // 严格递增，任何旧快照都不会再等于当前值
  for (let i = 1; i < stamps.length; i += 1) {
    assert.ok(stamps[i] > stamps[i - 1])
  }
  const current = guard.createStamp()
  for (const stale of stamps.slice(0, -1)) {
    assert.notEqual(stale, current)
  }
  assert.equal(stamps[stamps.length - 1], current)
  assert.equal(page.stillHere(), true)
})
