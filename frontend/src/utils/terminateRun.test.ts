/**
 * doc 11 §4.4/§4.7 终止按钮门控与计划变更规则的单元测试。
 * 运行：node --test src/utils/terminateRun.test.ts（在 frontend/ 下）。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import type { RunDetail, RunStepItem } from '../api/types'
import {
  TERMINATE_ACTIVE_HINT,
  canSubmitTerminate,
  plannedFinalStatus,
  plannedStepTransition,
  terminateButtonState,
} from './terminateRun.ts'

function run(overrides: Record<string, unknown> = {}): RunDetail {
  return {
    collection_run_id: 'r1',
    job_id: 'cj_1',
    job_name: null,
    workspace_id: null,
    revision_no: 1,
    trigger_type: 'manual',
    status: 'running',
    message: null,
    created_at: null,
    started_at: null,
    finished_at: null,
    run_type: 'collection',
    steps: [],
    ...overrides,
  } as RunDetail
}

function step(status: RunStepItem['status']): RunStepItem {
  return {
    step_name: status,
    status,
    message: null,
    step_config: null,
    input_manifest: null,
    started_at: null,
    finished_at: null,
  }
}

test('按钮仅 pending/running 可见，终态与空运行不可见', () => {
  assert.equal(terminateButtonState(run({ status: 'pending' })).visible, true)
  assert.equal(terminateButtonState(run({ status: 'running' })).visible, true)
  assert.equal(terminateButtonState(run({ status: 'success' })).visible, false)
  assert.equal(terminateButtonState(run({ status: 'partial_success' })).visible, false)
  assert.equal(terminateButtonState(run({ status: 'failed' })).visible, false)
  assert.equal(terminateButtonState(null).visible, false)
})

test('executor_active=true 时禁用并给出逃生口提示（§4.7）', () => {
  const state = terminateButtonState(run({ executor_active: true }))
  assert.equal(state.disabled, true)
  assert.equal(state.hint, TERMINATE_ACTIVE_HINT)
})

test('executor_active=false 可点且无附加提示', () => {
  const state = terminateButtonState(run({ executor_active: false }))
  assert.equal(state.disabled, false)
  assert.equal(state.hint, '')
})

test('executor_active 字段缺失不门控（退化为 v3 行为）', () => {
  const state = terminateButtonState(run({}))
  assert.equal(state.disabled, false)
  assert.equal(state.hint, '')
})

test('计划变更：running→失败、pending→被阻断、其余保持不变', () => {
  assert.equal(plannedStepTransition('running'), '失败')
  assert.equal(plannedStepTransition('pending'), '被阻断')
  assert.equal(plannedStepTransition('success'), '保持不变')
  assert.equal(plannedStepTransition('failed'), '保持不变')
  assert.equal(plannedStepTransition('blocked'), '保持不变')
})

test('汇总裁决：全部成功→success、有成功非全部→partial、无成功→failed', () => {
  assert.equal(plannedFinalStatus([step('success'), step('success')]), 'success')
  assert.equal(plannedFinalStatus([step('success'), step('running')]), 'partial_success')
  assert.equal(plannedFinalStatus([step('pending'), step('failed')]), 'failed')
  assert.equal(plannedFinalStatus([]), null)
})

test('确认按钮：原因非空且已勾选才可提交', () => {
  assert.equal(canSubmitTerminate('中断收尾', true), true)
  assert.equal(canSubmitTerminate('   ', true), false)
  assert.equal(canSubmitTerminate('', true), false)
  assert.equal(canSubmitTerminate('中断收尾', false), false)
})
