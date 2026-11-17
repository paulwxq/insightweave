import assert from 'node:assert/strict'
import { test } from 'node:test'
import { dimDraftFromConfig, sameDimTables } from './publishStepConfig.ts'

test('从配置接口初始化时保留 db_name，并将未指定字段转换为空数组', () => {
  const draft = dimDraftFromConfig([
    { schema_name: 'public', object_name: 'db_profile', embedding_col: 'db_name' },
    { schema_name: 'public', object_name: 'unset', embedding_col: null },
  ])
  assert.deepEqual(draft[0]?.embedding_col, ['db_name'])
  assert.deepEqual(draft[1]?.embedding_col, [])
})

test('仅调整表顺序或多选字段顺序，不算修改执行配置', () => {
  const left = [
    { schema_name: 'public', object_name: 'db_profile', embedding_col: ['db_name', 'host'] },
    { schema_name: 'public', object_name: 'other', embedding_col: ['name'] },
  ]
  const right = [left[1]!, { ...left[0]!, embedding_col: ['host', 'db_name'] }]
  assert.equal(sameDimTables(left, right), true)
  assert.deepEqual(left[0]?.embedding_col, ['db_name', 'host'])
})

test('用户选择其他字段、清空字段或删除表，都必须保留为用户修改', () => {
  const original = [{ schema_name: 'public', object_name: 'db_profile', embedding_col: ['db_name'] }]
  assert.equal(sameDimTables(original, [{ ...original[0]!, embedding_col: ['db_type'] }]), false)
  assert.equal(sameDimTables(original, [{ ...original[0]!, embedding_col: [] }]), false)
  assert.equal(sameDimTables(original, []), false)
})

test('不同 schema 的同名表不能视为同一份配置', () => {
  const original = [{ schema_name: 'public', object_name: 'db_profile', embedding_col: ['db_name'] }]
  assert.equal(sameDimTables(original, [{ ...original[0]!, schema_name: 'other' }]), false)
})
