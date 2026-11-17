/**
 * sessionList totalTurnsAll 状态测试（doc 12 §5 任务 2）。
 *
 * 执行真实 sessionList.ts / messages.ts 与真实 Vue ref，只替换
 * api/client 边界（含错误类，供真实 messages.ts 映射使用）；
 * 不复制请求代/去重状态机的假实现。
 */
const assert = require('node:assert/strict')
const { test } = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')

const FRONTEND_SRC = path.resolve(__dirname, '../..')
if (!FRONTEND_SRC.endsWith(path.join('src'))) throw new Error(`测试根目录解析异常：${FRONTEND_SRC}`)
const MODULES_DIR = path.join(FRONTEND_SRC, 'modules')

// ---------------------------------------------------------------- 假 API 边界

class Nl2sqlApiError extends Error {
  constructor(code, message) {
    super(message)
    this.code = code
    this.name = 'Nl2sqlApiError'
  }
}
class Nl2sqlNotConfiguredError extends Error {
  constructor(message) {
    super(message)
    this.code = 'not_configured'
    this.name = 'Nl2sqlNotConfiguredError'
  }
}

function makeSession(id, turnCount = 1) {
  return {
    session_id: id,
    source_id: 'db_src_1',
    title: '对话 ' + id,
    state: 'active',
    continuable: true,
    created_at: '2026-11-12T08:00:00+08:00',
    last_active_at: '2026-11-12T08:00:00+08:00',
    turn_count: turnCount,
    invalid_reason: null,
    invalidated_at: null,
  }
}

function makePage(overrides = {}) {
  return {
    items: [makeSession('s1'), makeSession('s2')],
    limit: 30,
    offset: 0,
    has_more: false,
    next_offset: null,
    total_all: 2,
    total_filtered: 2,
    total_turns_all: 5,
    ...overrides,
  }
}

function createApiStub() {
  const calls = { listSessions: [] }
  const handlers = {}
  const api = {
    Nl2sqlApiError,
    Nl2sqlNotConfiguredError,
    listSessions(params) {
      calls.listSessions.push({ ...params })
      return handlers.listSessions
        ? handlers.listSessions(params, calls.listSessions.length)
        : Promise.resolve(makePage())
    },
  }
  return { api, calls, handlers }
}

// ---------------------------------------------------------------- 模块加载

const STUB_FILES = new Set(['modules/nl2sql/api/client'])

function createEnv() {
  const stubs = createApiStub()
  const cache = new Map()
  const context = vm.createContext({
    console,
    setTimeout,
    clearTimeout,
    URL,
    URLSearchParams,
  })
  function loadFile(absNoExt) {
    const key = path.relative(FRONTEND_SRC, absNoExt).replaceAll('\\', '/')
    if (cache.has(key)) return cache.get(key)
    if (STUB_FILES.has(key)) {
      cache.set(key, stubs.api)
      return stubs.api
    }
    for (const candidate of [absNoExt, absNoExt + '.ts']) {
      const file = candidate
      if (!fs.existsSync(file) || !fs.statSync(file).isFile()) continue
      const source = fs.readFileSync(file, 'utf8')
      const js = ts.transpileModule(source, {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
      }).outputText
      const module = { exports: {} }
      cache.set(key, module.exports)
      const run = new vm.Script(`(function (exports, require, module) {\n${js}\n})`, {
        filename: file,
      })
      const customRequire = (specifier) => resolveModule(specifier, path.dirname(file))
      run.runInContext(context)(module.exports, customRequire, module)
      cache.set(key, module.exports)
      return module.exports
    }
    throw new Error(`无法加载模块：${key}`)
  }
  function resolveModule(specifier, fromDir) {
    if (specifier === 'vue') return vue
    if (specifier.startsWith('.')) {
      const abs = path.resolve(fromDir, specifier)
      const key = path.relative(FRONTEND_SRC, abs).replaceAll('\\', '/')
      if (STUB_FILES.has(key)) {
        if (!cache.has(key)) cache.set(key, stubs.api)
        return cache.get(key)
      }
      return loadFile(abs)
    }
    throw new Error(`未替换的运行时依赖：${specifier}`)
  }
  const sessionList = loadFile(path.join(MODULES_DIR, 'nl2sql/sessionList'))
  return { stubs, sessionList }
}

// ---------------------------------------------------------------- 用例

test('第一页成功写入统计；再次 loadFirst 立即回到未知，失败保持 null 并显示错误', async () => {
  const { stubs, sessionList } = createEnv()
  stubs.handlers.listSessions = () => Promise.resolve(makePage({ total_turns_all: 5 }))
  const list = sessionList.createSessionList()
  await list.loadFirst()
  assert.equal(list.totalTurnsAll.value, 5)

  let rejectSecond
  stubs.handlers.listSessions = () =>
    new Promise((_resolve, reject) => {
      rejectSecond = reject
    })
  const pending = list.loadFirst()
  assert.equal(list.totalTurnsAll.value, null, '新请求发起时应先重置为未知')
  rejectSecond(new Error('network down'))
  await pending
  assert.equal(list.totalTurnsAll.value, null, '失败后不得写入伪造值')
  assert.equal(list.loading.value, false)
  assert.equal(list.error.value, '会话列表读取失败，请稍后重试。')
})

test('loadPage 首次请求失败：统计保持 null、错误可见、列表不得写成成功', async () => {
  const { stubs, sessionList } = createEnv()
  stubs.handlers.listSessions = () => Promise.reject(new Error('first page down'))
  const list = sessionList.createSessionList()
  const applied = await list.loadPage({}, 1)
  assert.equal(applied, null)
  assert.equal(list.totalTurnsAll.value, null)
  assert.equal(list.totalAll.value, null)
  assert.equal(list.loading.value, false)
  assert.ok(list.error.value.length > 0)
  assert.deepEqual(Array.from(list.sessions.value), [])
})

test('筛选 A→B 乱序：B 完成后 A 的晚到成功不得覆盖统计、列表或错误状态', async () => {
  const { stubs, sessionList } = createEnv()
  const resolvers = []
  stubs.handlers.listSessions = () => new Promise((resolve) => resolvers.push(resolve))
  const list = sessionList.createSessionList()
  const a = list.loadFirst({ q: 'A' })
  const b = list.loadFirst({ q: 'B' })
  resolvers[1](makePage({ items: [makeSession('sB')], total_turns_all: 20 }))
  await b
  resolvers[0](makePage({ items: [makeSession('sA')], total_turns_all: 10 }))
  await a
  assert.equal(list.totalTurnsAll.value, 20)
  assert.deepEqual(
    Array.from(list.sessions.value, (item) => item.session_id),
    ['sB']
  )
  assert.equal(list.error.value, '')
  assert.equal(list.loading.value, false)
})

test('加载更多成功刷新全局统计；失败保留统计、原列表与待重试偏移', async () => {
  const { stubs, sessionList } = createEnv()
  stubs.handlers.listSessions = (_params, nth) => {
    if (nth === 1) {
      return Promise.resolve(
        makePage({ items: [makeSession('s1')], has_more: true, next_offset: 30, total_turns_all: 5 })
      )
    }
    if (nth === 2) {
      return Promise.resolve(
        makePage({
          items: [makeSession('s2')],
          offset: 30,
          has_more: true,
          next_offset: 60,
          total_turns_all: 6,
        })
      )
    }
    return Promise.reject(new Error('load more down'))
  }
  const list = sessionList.createSessionList()
  await list.loadFirst()
  assert.equal(list.totalTurnsAll.value, 5)
  await list.loadMore()
  assert.equal(list.totalTurnsAll.value, 6, '成功响应应刷新全局统计')
  assert.deepEqual(
    Array.from(list.sessions.value, (item) => item.session_id),
    ['s1', 's2']
  )
  await list.loadMore()
  assert.equal(list.totalTurnsAll.value, 6, '失败保留已确认统计')
  assert.deepEqual(
    Array.from(list.sessions.value, (item) => item.session_id),
    ['s1', 's2'],
    '失败保留原列表'
  )
  assert.equal(list.nextOffset.value, 60, '失败保留待重试偏移')
  assert.equal(list.loading.value, false)
  assert.equal(list.error.value, '更多会话加载失败，请稍后重试。')
})

test('越界页二次请求：offset 依序 588/6，返回实际页码并采用第二响应的列表与统计', async () => {
  const { stubs, sessionList } = createEnv()
  const offsets = []
  stubs.handlers.listSessions = (params) => {
    offsets.push(params.offset)
    if (offsets.length === 1) {
      return Promise.resolve(
        makePage({
          limit: 6,
          offset: 588,
          items: [makeSession('stale')],
          has_more: true,
          next_offset: 589,
          total_all: 8,
          total_filtered: 8,
          total_turns_all: 10,
        })
      )
    }
    return Promise.resolve(
      makePage({
        limit: 6,
        offset: 6,
        items: [makeSession('s7'), makeSession('s8')],
        has_more: false,
        next_offset: null,
        total_all: 8,
        total_filtered: 8,
        total_turns_all: 12,
      })
    )
  }
  const list = sessionList.createSessionList(6)
  const applied = await list.loadPage({}, 99)
  assert.equal(applied, 2, '应返回实际页码 2')
  assert.deepEqual(offsets, [588, 6])
  assert.equal(list.totalTurnsAll.value, 12, '采用第二响应的统计值')
  assert.deepEqual(
    Array.from(list.sessions.value, (item) => item.session_id),
    ['s7', 's8']
  )
  assert.equal(list.hasMore.value, false)
  assert.equal(list.nextOffset.value, null)
  assert.equal(list.error.value, '')
})
