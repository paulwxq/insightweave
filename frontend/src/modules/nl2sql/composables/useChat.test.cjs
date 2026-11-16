/**
 * useChat 首次发送创建会话流程测试（doc 11 §5.1）。
 *
 * 执行真实 useChat.ts / pageGuard.ts / format.ts / messages.ts / queryHomeLink.ts
 * 与真实 Vue 运行时（ref/computed/onUnmounted/createRenderer 宿主），
 * 只替换 API 与路由边界；不复制协调算法的假实现。
 */
const assert = require('node:assert/strict')
const { test } = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')

const FRONTEND_SRC = path.resolve(__dirname, '../../..')
if (!FRONTEND_SRC.endsWith(path.join('src'))) throw new Error(`测试根目录解析异常：${FRONTEND_SRC}`)
const MODULES_DIR = path.join(FRONTEND_SRC, 'modules')

const SRC_A = 'db_src_1'
const SRC_B = 'db_src_2'
const CREATE_PENDING_COPY = '会话创建结果尚未确认，请勿重复发送，可稍后从查询历史查看。'

// ---------------------------------------------------------------- 假 API 边界

class Nl2sqlApiError extends Error {
  constructor(code, message, httpStatus = null, data = null) {
    super(message)
    this.code = code
    this.httpStatus = httpStatus
    this.data = data
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
class ApiError extends Error {}

function makeResult(answer = 'ok') {
  return {
    status: 'completed',
    answer,
    error_code: null,
    evidence: [],
    findings: [],
    clarifications: [],
    stats: { elapsed_ms: 1 },
    history_saved: true,
  }
}

function createApiStub() {
  let sessionSeq = 0
  const calls = { createSession: [], getReadiness: [], submitQuery: [], getSession: [], listTurns: [] }
  const handlers = {}
  const makeSession = (sourceId, overrides = {}) => ({
    session_id: `s_${++sessionSeq}`,
    source_id: sourceId,
    title: '新对话',
    state: 'active',
    continuable: true,
    created_at: '2026-11-12T08:00:00+08:00',
    last_active_at: '2026-11-12T08:00:00+08:00',
    turn_count: 0,
    invalid_reason: null,
    invalidated_at: null,
    ...overrides,
  })
  const api = {
    Nl2sqlApiError,
    Nl2sqlNotConfiguredError,
    QUERY_TIMEOUT_MS: 1000,
    nl2sqlBaseUrl: () => 'http://stub',
    resetNl2sqlBaseUrlCache: () => {},
    getReadiness(sourceId) {
      calls.getReadiness.push(sourceId)
      return handlers.getReadiness
        ? handlers.getReadiness(sourceId, calls.getReadiness.length)
        : Promise.resolve({ source_id: sourceId, compatible: true, ready: true, checked_at: '', checks: [] })
    },
    createSession(sourceId) {
      calls.createSession.push(sourceId)
      return handlers.createSession ? handlers.createSession(sourceId, calls.createSession.length) : Promise.resolve(makeSession(sourceId))
    },
    getSession(sessionId) {
      calls.getSession.push(sessionId)
      return handlers.getSession ? handlers.getSession(sessionId, calls.getSession.length) : Promise.resolve(makeSession(SRC_A, { session_id: sessionId }))
    },
    listTurns(sessionId, limit, offset) {
      calls.listTurns.push([sessionId, limit, offset])
      return Promise.resolve({ items: [], has_more: false, next_offset: null, offset })
    },
    submitQuery(sessionId, question, sourceId) {
      calls.submitQuery.push([sessionId, question, sourceId])
      return handlers.submitQuery ? handlers.submitQuery(sessionId, question, sourceId, calls.submitQuery.length) : Promise.resolve(makeResult())
    },
  }
  return { api, calls, handlers, makeSession }
}

function createRouterStub(initial) {
  const currentRoute = vue.ref({
    name: initial.name,
    params: { ...(initial.params || {}) },
    query: { ...(initial.query || {}) },
  })
  const ops = { replace: [], push: [] }
  let failNextReplace = false
  let rejectNextReplace = false
  const router = {
    currentRoute,
    controls: {
      failNextReplace() {
        failNextReplace = true
      },
      rejectNextReplace() {
        rejectNextReplace = true
      },
    },
    async replace(to) {
      ops.replace.push(to)
      if (rejectNextReplace) {
        rejectNextReplace = false
        throw new Error('navigation guard error (stub)')
      }
      if (failNextReplace) {
        failNextReplace = false
        return { type: 16, to }
      }
      currentRoute.value = {
        name: to.name,
        params: { ...(to.params || {}) },
        query: { ...(to.query || {}) },
      }
      return undefined
    },
    async push(to) {
      ops.push.push(to)
      currentRoute.value = {
        name: to.name,
        params: { ...(to.params || {}) },
        query: { ...(to.query || {}) },
      }
      return undefined
    },
  }
  return { router, ops }
}

// ---------------------------------------------------------------- 模块加载

const STUB_FILES = new Set([
  'modules/nl2sql/api/client',
  'modules/nl2sql/api/types',
  'api/dataSources',
  'api/http',
  'api/types',
])

function createEnv() {
  const stubs = createApiStub()
  const dataSources = {
    listDataSources: () =>
      Promise.resolve({
        items: [
          { source_id: SRC_A, display_name: 'A库', db_type: 'postgresql', db_name: 'a', selected_schemas: ['public'], is_active: true },
          { source_id: SRC_B, display_name: 'B库', db_type: 'postgresql', db_name: 'b', selected_schemas: ['public'], is_active: true },
        ],
      }),
  }
  const cache = new Map()
  const context = vm.createContext({
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    URL,
    URLSearchParams,
  })
  function loadFile(absNoExt) {
    const key = path.relative(FRONTEND_SRC, absNoExt).replaceAll('\\', '/')
    if (cache.has(key)) return cache.get(key)
    if (STUB_FILES.has(key)) {
      const module = { exports: stubModule(key) }
      cache.set(key, module.exports)
      return module.exports
    }
    for (const candidate of [absNoExt, absNoExt + '.ts', absNoExt + '/index.ts']) {
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
  function stubModule(key) {
    if (key === 'modules/nl2sql/api/client') return stubs.api
    if (key === 'modules/api/dataSources') return dataSources
    if (key === 'modules/api/http') return { ApiError }
    return {}
  }
  function resolveModule(specifier, fromDir) {
    if (specifier === 'vue') return vue
    if (specifier.startsWith('.')) {
      const abs = path.resolve(fromDir, specifier)
      const key = path.relative(FRONTEND_SRC, abs).replaceAll('\\', '/')
      if (STUB_FILES.has(key)) {
        if (!cache.has(key)) cache.set(key, stubModule(key))
        return cache.get(key)
      }
      return loadFile(abs)
    }
    throw new Error(`未替换的运行时依赖：${specifier}`)
  }
  const useChat = loadFile(path.join(MODULES_DIR, 'nl2sql/composables/useChat'))
  return { stubs, useChat, vue }
}

// ---------------------------------------------------------------- 挂载宿主

function createTestRenderer() {
  const nodeOps = {
    insert(child, parent, anchor) {
      child.parentNode = parent
      parent.children = parent.children || []
      const index = anchor ? parent.children.indexOf(anchor) : -1
      if (index === -1) parent.children.push(child)
      else parent.children.splice(index, 1, child)
    },
    remove(child) {
      const parent = child.parentNode
      if (parent?.children) parent.children.splice(parent.children.indexOf(child), 1)
      child.parentNode = null
    },
    createElement: (tag) => ({ tag, children: [], parentNode: null, props: {} }),
    createText: (text) => ({ text, parentNode: null }),
    createComment: (text) => ({ text: '', comment: text, parentNode: null }),
    setText(node, text) {
      node.text = text
    },
    setElementText(el, text) {
      el.children = [String(text)]
    },
    parentNode: (node) => node.parentNode ?? null,
    nextSibling(node) {
      const parent = node.parentNode
      if (!parent?.children) return null
      const siblings = parent.children
      return siblings[siblings.indexOf(node) + 1] ?? null
    },
    querySelector: () => null,
    setScopeId: () => {},
    cloneNode: (node) => ({ ...node }),
    insertStaticContent: () => [null, null],
  }
  return vue.createRenderer(nodeOps)
}

const renderer = createTestRenderer()

function mountChat(env, router) {
  let chat = null
  const app = renderer.createApp({
    setup() {
      chat = env.useChat.useNl2sqlChat(router)
      return () => env.vue.h('div', null, 'host')
    },
  })
  app.mount({ tag: 'root', children: [], parentNode: null, props: {} })
  return { chat, unmount: () => app.unmount() }
}

async function createMounted(options = {}) {
  const env = createEnv()
  const initial = options.initial || { name: 'nl2sql-query', params: {}, query: {} }
  const routerBundle = createRouterStub(initial)
  const mounted = mountChat(env, routerBundle.router)
  return { env, router: routerBundle.router, ops: routerBundle.ops, chat: mounted.chat, unmount: mounted.unmount }
}

const flush = async (rounds = 12) => {
  for (let i = 0; i < rounds; i++) await new Promise((resolve) => setImmediate(resolve))
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

async function readyOn(env, chat, sourceId = SRC_A) {
  env.stubs.handlers.getReadiness = () =>
    Promise.resolve({ source_id: sourceId, compatible: true, ready: true, checked_at: '', checks: [] })
  chat.checkReadiness(sourceId)
  await flush()
}

// ---------------------------------------------------------------- 用例

test('校验与输入：idle/checking/blocked/error 不可输入；ready 可输入；校验成功不创建不查询', async () => {
  const t = await createMounted()
  try {
    const { chat, env } = t
    assert.equal(chat.selectedSourceId.value, '')
    assert.equal(chat.canCompose.value, false)

    const gate = deferred()
    env.stubs.handlers.getReadiness = () => gate.promise
    chat.checkReadiness(SRC_A)
    await flush(2)
    assert.equal(chat.readinessState.value, 'checking')
    assert.equal(chat.canCompose.value, false)

    gate.resolve({ source_id: SRC_A, compatible: true, ready: true, checked_at: '', checks: [] })
    await flush()
    assert.equal(chat.readinessState.value, 'ready')
    assert.equal(chat.canCompose.value, true)
    assert.equal(env.stubs.calls.createSession.length, 0)
    assert.equal(env.stubs.calls.submitQuery.length, 0)

    // blocked 与 error 不可输入
    env.stubs.handlers.getReadiness = () =>
      Promise.resolve({ source_id: SRC_B, compatible: true, ready: false, checked_at: '', checks: [] })
    chat.checkReadiness(SRC_B)
    await flush()
    assert.equal(chat.readinessState.value, 'blocked')
    assert.equal(chat.canCompose.value, false)

    env.stubs.handlers.getReadiness = () =>
      Promise.reject(new Nl2sqlApiError('dependency_unavailable', '依赖不可用'))
    chat.checkReadiness(SRC_A)
    await flush()
    assert.equal(chat.readinessState.value, 'error')
    assert.equal(chat.canCompose.value, false)
    assert.equal(env.stubs.calls.createSession.length, 0)
    assert.equal(env.stubs.calls.submitQuery.length, 0)
  } finally {
    t.unmount()
  }
})

test('快速换库：换库立即禁用，旧库真正的晚到结果不能开放输入，草稿保留', async () => {
  const t = await createMounted()
  try {
    const { chat, env } = t
    // 两个 readiness 请求都未结束：A 先发起但保持未决，切换 B 后 B 先返回，A 再晚到
    const gates = {}
    env.stubs.handlers.getReadiness = (sourceId) => {
      gates[sourceId] = gates[sourceId] || deferred()
      return gates[sourceId].promise
    }
    chat.checkReadiness(SRC_A)
    await flush(2)
    assert.equal(chat.readinessState.value, 'checking')
    chat.questionDraft.value = '半句待续的问题'

    chat.checkReadiness(SRC_B)
    await flush(2)
    assert.equal(chat.selectedSourceId.value, SRC_B)
    assert.equal(chat.readinessState.value, 'checking')
    assert.equal(chat.canCompose.value, false)

    gates[SRC_B].resolve({ source_id: SRC_B, compatible: true, ready: true, checked_at: '', checks: [] })
    await flush()
    assert.equal(chat.readinessState.value, 'ready')
    assert.equal(chat.selectedSourceId.value, SRC_B)
    assert.equal(chat.canCompose.value, true)

    // A 的响应此刻才到达（真正晚到），不得影响 B 的就绪状态
    gates[SRC_A].resolve({ source_id: SRC_A, compatible: true, ready: true, checked_at: '', checks: [] })
    await flush()
    assert.equal(chat.selectedSourceId.value, SRC_B)
    assert.equal(chat.readinessState.value, 'ready')
    assert.equal(chat.canCompose.value, true)
    assert.equal(chat.questionDraft.value, '半句待续的问题')
  } finally {
    t.unmount()
  }
})

test('首次发送成功：创建→地址替换→提问严格有序各一次，使用返回的真实会话', async () => {
  const t = await createMounted({
    initial: { name: 'nl2sql-query', params: {}, query: { source_id: SRC_A, home: '/query?q=销售' } },
  })
  try {
    const { chat, env, ops } = t
    await readyOn(env, chat, SRC_A)
    chat.questionDraft.value = '统计上个月的销售额'

    const events = []
    const queryGate = deferred()
    const originalReplace = t.router.replace.bind(t.router)
    t.router.replace = async (to) => {
      events.push('replace')
      return originalReplace(to)
    }
    env.stubs.handlers.createSession = () => {
      events.push('create')
      return Promise.resolve(env.stubs.makeSession(SRC_A))
    }
    env.stubs.handlers.submitQuery = () => {
      events.push('query')
      return queryGate.promise
    }
    const pending = chat.sendQuestion('统计上个月的销售额')
    await flush(2)
    assert.deepEqual(events, ['create', 'replace', 'query'], '创建→替换→提问严格有序')
    assert.ok(ops.replace.length === 1)
    const replaceTo = ops.replace[0]
    assert.equal(replaceTo.name, 'nl2sql-query-session')
    assert.equal(replaceTo.query.source_id, undefined, '不得把预选 source_id 带入会话地址')
    assert.equal(replaceTo.query.home, '/query?q=%E9%94%80%E5%94%AE', 'home 保留且经规范化（URL 编码）')
    const sessionId = chat.currentSessionId.value
    assert.notEqual(sessionId, '')
    assert.equal(chat.hasCurrentSession(sessionId), true)
    assert.equal(chat.hasCurrentSession('s_other'), false)
    assert.equal(env.stubs.calls.getSession.length, 0, '接管不额外请求详情')
    assert.equal(env.stubs.calls.listTurns.length, 0, '接管不额外加载历史')

    queryGate.resolve(makeResult())
    await pending
    await flush()
    assert.deepEqual(events, ['create', 'replace', 'query'])
    assert.equal(env.stubs.calls.createSession.length, 1)
    assert.equal(env.stubs.calls.submitQuery.length, 1)
    assert.equal(env.stubs.calls.submitQuery[0][0], sessionId)
    assert.equal(env.stubs.calls.submitQuery[0][1], '统计上个月的销售额')
    assert.equal(env.stubs.calls.submitQuery[0][2], SRC_A)
    assert.equal(chat.askState.value, 'done')
    assert.equal(chat.questionDraft.value, '', '提交后清空所提交草稿')
    assert.equal(chat.creatingSession.value, false)
    assert.equal(chat.busy.value, false)
  } finally {
    t.unmount()
  }
})

test('连续 Enter/双击：第一段 await 前已置忙碌，不重复创建或提交', async () => {
  const t = await createMounted()
  try {
    const { chat, env } = t
    await readyOn(env, chat)
    const gate = deferred()
    env.stubs.handlers.createSession = () => gate.promise
    const first = chat.sendQuestion('双击问题')
    const second = chat.sendQuestion('双击问题')
    assert.equal(chat.creatingSession.value, true)
    gate.resolve(env.stubs.makeSession(SRC_A))
    await first
    await second
    await flush()
    assert.equal(env.stubs.calls.createSession.length, 1)
    assert.equal(env.stubs.calls.submitQuery.length, 1)
  } finally {
    t.unmount()
  }
})

test('创建明确失败：不导航不提问，草稿保留，状态允许手动再发', async () => {
  const t = await createMounted()
  try {
    const { chat, env, ops } = t
    await readyOn(env, chat)
    chat.questionDraft.value = '失败后重发的问题'
    env.stubs.handlers.createSession = () =>
      Promise.reject(new Nl2sqlApiError('source_not_ready', '暂未就绪', 409))
    await chat.sendQuestion('失败后重发的问题')
    assert.equal(env.stubs.calls.createSession.length, 1)
    assert.equal(ops.replace.length, 0)
    assert.equal(env.stubs.calls.submitQuery.length, 0)
    assert.equal(chat.sessionsNotice.value, '数据源暂未就绪，请检查发布状态后重试。')
    assert.equal(chat.questionDraft.value, '失败后重发的问题')
    assert.equal(chat.creatingSession.value, false)
    assert.equal(chat.canCompose.value, true, '失败后按 readiness 恢复可输入')

    env.stubs.handlers.createSession = () => Promise.resolve(env.stubs.makeSession(SRC_A))
    await chat.sendQuestion('失败后重发的问题')
    assert.equal(env.stubs.calls.createSession.length, 2)
    assert.equal(env.stubs.calls.submitQuery.length, 1)
    assert.equal(chat.askState.value, 'done')
  } finally {
    t.unmount()
  }
})

test('创建超时与网络失败：统一显示创建结果不确定文案，不自动重试，草稿保留', async () => {
  for (const code of ['timeout', 'network_error']) {
    const t = await createMounted()
    try {
      const { chat, env, ops } = t
      await readyOn(env, chat)
      chat.questionDraft.value = '超时场景问题'
      env.stubs.handlers.createSession = () =>
        Promise.reject(new Nl2sqlApiError(code, code === 'timeout' ? '结果尚未确认' : '无法连接'))
      await chat.sendQuestion('超时场景问题')
      assert.equal(chat.sessionsNotice.value, CREATE_PENDING_COPY, `${code} 应显示统一创建文案`)
      assert.equal(ops.replace.length, 0)
      assert.equal(env.stubs.calls.submitQuery.length, 0)
      assert.equal(env.stubs.calls.createSession.length, 1, '不自动重试')
      assert.equal(chat.questionDraft.value, '超时场景问题')
      assert.equal(chat.creatingSession.value, false)
    } finally {
      t.unmount()
    }
  }
})

test('创建后导航失败：不提问不清草稿；重试只补路由与提问，不再创建', async () => {
  const t = await createMounted()
  try {
    const { chat, env, ops, router } = t
    await readyOn(env, chat)
    chat.questionDraft.value = '导航失败的问题'
    router.controls.failNextReplace()
    await chat.sendQuestion('导航失败的问题')
    assert.equal(env.stubs.calls.createSession.length, 1)
    assert.equal(env.stubs.calls.submitQuery.length, 0, '导航失败不得提交首问')
    const sessionId = chat.currentSessionId.value
    assert.notEqual(sessionId, '')
    assert.equal(chat.hasCurrentSession(sessionId), true, '已创建会话保留')
    assert.equal(chat.questionDraft.value, '导航失败的问题')
    assert.ok(chat.askNotice.value.includes('页面切换失败'), '使用始终可见的 askNotice 提示')
    assert.equal(chat.creatingSession.value, false)

    await chat.sendQuestion('导航失败的问题')
    assert.equal(env.stubs.calls.createSession.length, 1, '重试不得再创建会话')
    assert.equal(ops.replace.length, 2)
    assert.equal(env.stubs.calls.submitQuery.length, 1)
    assert.equal(env.stubs.calls.submitQuery[0][0], sessionId)
    assert.equal(router.currentRoute.value.params.sessionId, sessionId)
    assert.equal(chat.askState.value, 'done')
  } finally {
    t.unmount()
  }
})

test('创建返回后状态不允许提问：不调用 query，展示既有阻断原因，草稿保留', async () => {
  const t = await createMounted()
  try {
    const { chat, env, ops } = t
    await readyOn(env, chat)
    chat.questionDraft.value = '状态受阻的问题'
    env.stubs.handlers.createSession = () =>
      Promise.resolve(env.stubs.makeSession(SRC_A, { continuable: false }))
    await chat.sendQuestion('状态受阻的问题')
    assert.equal(env.stubs.calls.createSession.length, 1)
    assert.equal(ops.replace.length, 0)
    assert.equal(env.stubs.calls.submitQuery.length, 0)
    assert.equal(chat.hasCurrentSession(chat.currentSessionId.value), true)
    assert.equal(chat.inputBlockedReason.value, '上一操作尚未确认，请刷新查看。')
    assert.equal(chat.questionDraft.value, '状态受阻的问题')
    assert.equal(chat.creatingSession.value, false)
    assert.equal(chat.busy.value, false)
  } finally {
    t.unmount()
  }
})

test('旧创建归属：创建时卸载、卸载后重进、同页预选变化、同组件切已有会话均隔离', async () => {
  // a) 创建中卸载：迟到成功不跳转不接管
  {
    const t = await createMounted()
    const { chat, env, ops } = t
    await readyOn(env, chat)
    const gate = deferred()
    env.stubs.handlers.createSession = () => gate.promise
    const pending = chat.sendQuestion('卸载中的问题')
    t.unmount()
    gate.resolve(env.stubs.makeSession(SRC_A))
    await pending
    await flush()
    assert.equal(ops.replace.length, 0)
    assert.equal(env.stubs.calls.submitQuery.length, 0)
  }
  // b) 卸载后重进：旧实例归属永久失效，新实例可用且不被旧回调污染
  {
    const env = createEnv()
    const bundle = createRouterStub({ name: 'nl2sql-query', params: {}, query: {} })
    const first = mountChat(env, bundle.router)
    const chat1 = first.chat
    const gate = deferred()
    env.stubs.handlers.createSession = () => gate.promise
    chat1.checkReadiness(SRC_A)
    await flush()
    const pending = chat1.sendQuestion('旧实例问题')
    first.unmount()

    const second = mountChat(env, bundle.router)
    const chat2 = second.chat
    assert.equal(chat2.currentSessionId.value, '')
    gate.resolve(env.stubs.makeSession(SRC_A))
    await pending
    await flush()
    assert.equal(bundle.ops.replace.length, 0, '旧实例迟到成功不得跳转')
    assert.equal(chat2.currentSessionId.value, '', '旧实例迟到成功不得接管新实例')
    assert.equal(env.stubs.calls.submitQuery.length, 0)
    second.unmount()
  }
  // c) 同页预选变化（创建代失效路径）
  {
    const t = await createMounted()
    const { chat, env, ops } = t
    await readyOn(env, chat)
    chat.questionDraft.value = '预选变化的问题'
    const gate = deferred()
    env.stubs.handlers.createSession = () => gate.promise
    const pending = chat.sendQuestion('预选变化的问题')
    chat.invalidatePendingCreate()
    assert.equal(chat.creatingSession.value, false, '作废时同步释放创建忙碌')
    gate.resolve(env.stubs.makeSession(SRC_A))
    await pending
    await flush()
    assert.equal(ops.replace.length, 0)
    assert.equal(env.stubs.calls.submitQuery.length, 0)
    assert.equal(chat.currentSessionId.value, '', '迟到成功不接管')
    assert.equal(chat.questionDraft.value, '预选变化的问题', '预选变化保留草稿')
    assert.equal(chat.creatingSession.value, false)
  }
  // d) 创建中同组件切到已有会话（resetCurrentSession 递增 requestToken 路径）
  {
    const t = await createMounted()
    const { chat, env, ops } = t
    await readyOn(env, chat)
    chat.questionDraft.value = '切会话期间的问题'
    const gate = deferred()
    env.stubs.handlers.createSession = () => gate.promise
    const pending = chat.sendQuestion('切会话期间的问题')
    await chat.openSession('s_existing')
    await flush()
    assert.equal(chat.currentSessionId.value, 's_existing')
    assert.equal(chat.creatingSession.value, false, 'resetCurrentSession 同步释放创建忙碌')
    assert.equal(chat.questionDraft.value, '', '打开其他会话清空草稿')
    gate.resolve(env.stubs.makeSession(SRC_A))
    await pending
    await flush()
    assert.equal(chat.currentSessionId.value, 's_existing', '迟到创建不得改写新会话状态')
    assert.equal(ops.replace.length, 0)
    assert.equal(env.stubs.calls.submitQuery.length, 0)
  }
})

test('首问失败后再发：session_busy 恢复草稿、不重复创建；查询超时不自动重发', async () => {
  const t = await createMounted()
  try {
    const { chat, env } = t
    await readyOn(env, chat)
    chat.questionDraft.value = '首问被拒'
    env.stubs.handlers.submitQuery = () =>
      Promise.reject(new Nl2sqlApiError('session_busy', '忙碌', 409))
    await chat.sendQuestion('首问被拒')
    const sessionId = chat.currentSessionId.value
    assert.equal(env.stubs.calls.createSession.length, 1)
    assert.equal(env.stubs.calls.submitQuery.length, 1)
    assert.equal(chat.questionDraft.value, '首问被拒', 'session_busy 且草稿为空时恢复本次问题')
    assert.equal(chat.currentTurn.value, null)
    assert.equal(chat.askNotice.value, '本次未执行，会话正在处理另一项操作，请稍后手动重试。')
    assert.equal(chat.busy.value, false)

    env.stubs.handlers.submitQuery = () =>
      Promise.reject(new Nl2sqlApiError('timeout', '结果尚未确认', null))
    await chat.sendQuestion('首问被拒')
    assert.equal(env.stubs.calls.createSession.length, 1, '再发沿用已创建会话')
    assert.equal(env.stubs.calls.submitQuery.length, 2)
    assert.equal(env.stubs.calls.submitQuery[1][0], sessionId)
    await flush(4)
    assert.equal(env.stubs.calls.submitQuery.length, 2, '查询超时不自动重发')
    assert.equal(chat.askState.value, 'error')
    assert.equal(chat.currentTurn.value?.question, '首问被拒', '已展示的问题保留在当前轮')
    assert.equal(chat.questionDraft.value, '', '查询超时不恢复草稿')
  } finally {
    t.unmount()
  }
})

test('已有会话：发送不创建；失效/只读阻断生效', async () => {
  const t = await createMounted({
    initial: { name: 'nl2sql-query-session', params: { sessionId: 's_live' }, query: {} },
  })
  try {
    const { chat, env } = t
    await chat.openSession('s_live')
    await flush()
    assert.equal(chat.currentSessionId.value, 's_live')
    assert.equal(chat.canCompose.value, true)
    await chat.sendQuestion('已有会话问题')
    assert.equal(env.stubs.calls.createSession.length, 0, '已有会话发送不创建')
    assert.equal(env.stubs.calls.submitQuery.length, 1)
    assert.equal(env.stubs.calls.submitQuery[0][0], 's_live')

    env.stubs.handlers.getSession = () =>
      Promise.resolve(env.stubs.makeSession(SRC_A, { session_id: 's_dead', state: 'invalidated', invalid_reason: 'knowledge_changed', continuable: false }))
    await chat.openSession('s_dead')
    await flush()
    assert.equal(chat.canCompose.value, false)
    assert.notEqual(chat.inputBlockedReason.value, '')
    await chat.sendQuestion('失效会话问题')
    assert.equal(env.stubs.calls.submitQuery.length, 1, '失效会话不得提问')
  } finally {
    t.unmount()
  }
})

test('问题长度：空白不提交；1000 非BMP码点可提交；1001 拒绝且不创建', async () => {
  const t = await createMounted()
  try {
    const { chat, env, ops } = t
    await readyOn(env, chat)
    await chat.sendQuestion('   ')
    assert.equal(env.stubs.calls.createSession.length, 0)

    const tooLong = '😀'.repeat(1001)
    await chat.sendQuestion(tooLong)
    assert.equal(env.stubs.calls.createSession.length, 0)
    assert.equal(ops.replace.length, 0)

    const exact = '😀'.repeat(1000)
    await chat.sendQuestion(exact)
    assert.equal(env.stubs.calls.createSession.length, 1)
    assert.equal(env.stubs.calls.submitQuery.length, 1)
    assert.equal(env.stubs.calls.submitQuery[0][1].length, 2000, '按 UTF-16 长度仍完整送达原文')
  } finally {
    t.unmount()
  }
})

test('更换来源清掉上一次创建提示，不遮住新校验结果', async () => {
  const t = await createMounted()
  try {
    const { chat, env } = t
    await readyOn(env, chat)
    env.stubs.handlers.createSession = () =>
      Promise.reject(new Nl2sqlApiError('timeout', '结果尚未确认'))
    await chat.sendQuestion('提示清理问题')
    assert.equal(chat.sessionsNotice.value, CREATE_PENDING_COPY)
    chat.checkReadiness(SRC_B)
    assert.equal(chat.sessionsNotice.value, '', '换源即清旧创建提示')
    await flush()
    assert.equal(chat.readinessState.value, 'ready')
  } finally {
    t.unmount()
  }
})

// ------------------------------------------------------------ Codex R1 审核回归

/** 模拟 QueryView preset 监听在预选变化时的调用序列（doc11 R1-01 修复接线）。 */
function simulatePresetChange(t, chat, newSourceId) {
  chat.invalidatePendingCreate()
  chat.releaseUnalignedTakeover()
  if (newSourceId === '') {
    chat.resetReadiness()
    t.router.currentRoute.value = {
      name: 'nl2sql-query',
      params: {},
      query: {},
    }
  } else {
    t.router.currentRoute.value = {
      name: 'nl2sql-query',
      params: {},
      query: { source_id: newSourceId },
    }
    void chat.checkReadiness(newSourceId)
  }
}

async function readySource(env, sourceId) {
  env.stubs.handlers.getReadiness = (id) =>
    Promise.resolve({ source_id: id, compatible: true, ready: true, checked_at: '', checks: [] })
}

test('R1-01a 导航失败后换预选：放弃未交接的接管会话，新问题只发到新库新会话', async () => {
  const t = await createMounted({
    initial: { name: 'nl2sql-query', params: {}, query: { source_id: SRC_A } },
  })
  try {
    const { chat, env, ops } = t
    await readySource(env, SRC_A)
    chat.checkReadiness(SRC_A)
    await flush()
    t.router.controls.failNextReplace()
    await chat.sendQuestion('导航失败后换库的问题')
    const firstSessionId = chat.currentSessionId.value
    assert.equal(chat.hasCurrentSession(firstSessionId), true, '前置：A 会话已接管')

    // 预选 A→B：放弃接管、保留草稿、校验 B
    chat.questionDraft.value = '导航失败后换库的问题'
    await readySource(env, SRC_B)
    simulatePresetChange(t, chat, SRC_B)
    await flush()
    assert.equal(chat.currentSessionId.value, '', '换预选后释放旧绑定')
    assert.equal(chat.sessionInfo.value, null)
    assert.equal(chat.questionDraft.value, '导航失败后换库的问题', '草稿保留')
    assert.equal(env.stubs.calls.createSession.length, 1, '释放不新建')

    await chat.sendQuestion('导航失败后换库的问题')
    assert.equal(env.stubs.calls.createSession.length, 2, '新问题创建 B 的新会话')
    const secondSessionId = chat.currentSessionId.value
    assert.notEqual(secondSessionId, firstSessionId)
    assert.equal(env.stubs.calls.submitQuery.length, 1)
    assert.equal(env.stubs.calls.submitQuery[0][0], secondSessionId)
    assert.equal(env.stubs.calls.submitQuery[0][2], SRC_B, '提问使用新来源')
    assert.equal(ops.replace.length, 2)
  } finally {
    t.unmount()
  }
})

test('R1-01b 替换等待期换预选：旧流程取消后不接管不提问，状态可用于新库', async () => {
  const t = await createMounted({
    initial: { name: 'nl2sql-query', params: {}, query: { source_id: SRC_A } },
  })
  try {
    const { chat, env } = t
    await readySource(env, SRC_A)
    chat.checkReadiness(SRC_A)
    await flush()
    const originalReplace = t.router.replace.bind(t.router)
    const navGate = deferred()
    t.router.replace = async (to) => {
      const outcome = await navGate.promise
      return outcome
    }
    const pending = chat.sendQuestion('替换期间换库的问题')
    await flush(2)
    assert.equal(chat.hasCurrentSession(chat.currentSessionId.value), true, '接管已发生、导航等待中')

    await readySource(env, SRC_B)
    simulatePresetChange(t, chat, SRC_B)
    await flush()
    assert.equal(chat.currentSessionId.value, '', '换预选立即释放')

    navGate.resolve({ type: 16 }) // 原导航被新导航取消
    await pending
    await flush()
    t.router.replace = originalReplace
    assert.equal(chat.currentSessionId.value, '', '迟到导航结果不改写释放后的状态')
    assert.equal(env.stubs.calls.submitQuery.length, 0, '不得提问')
    await chat.sendQuestion('替换期间换库的问题')
    assert.equal(env.stubs.calls.createSession.length, 2)
    assert.equal(env.stubs.calls.submitQuery[0][2], SRC_B)
  } finally {
    t.unmount()
  }
})

test('R1-01c 导航失败后转裸地址：释放绑定并禁用输入直到重选', async () => {
  const t = await createMounted({
    initial: { name: 'nl2sql-query', params: {}, query: { source_id: SRC_A } },
  })
  try {
    const { chat, env } = t
    await readySource(env, SRC_A)
    chat.checkReadiness(SRC_A)
    await flush()
    chat.questionDraft.value = '转裸地址的问题'
    t.router.controls.failNextReplace()
    await chat.sendQuestion('转裸地址的问题')
    assert.equal(chat.currentSessionId.value !== '', true)

    simulatePresetChange(t, chat, '')
    await flush()
    assert.equal(chat.currentSessionId.value, '')
    assert.equal(chat.selectedSourceId.value, '')
    assert.equal(chat.canCompose.value, false, '裸地址禁用直到重选')

    await chat.sendQuestion('转裸地址的问题')
    assert.equal(env.stubs.calls.createSession.length, 1, '禁用状态不创建')

    await readySource(env, SRC_B)
    simulatePresetChange(t, chat, SRC_B)
    await flush()
    assert.equal(chat.canCompose.value, true)
    assert.equal(chat.questionDraft.value, '转裸地址的问题', '草稿仍保留')
  } finally {
    t.unmount()
  }
})

test('R1-02a replace 抛异常：视为导航失败，写可见提示、保留会话与草稿、重试不重建', async () => {
  const t = await createMounted()
  try {
    const { chat, env, ops } = t
    await readyOn(env, chat)
    chat.questionDraft.value = '导航抛错的问题'
    t.router.controls.rejectNextReplace()
    await chat.sendQuestion('导航抛错的问题') // 不得 reject
    assert.equal(env.stubs.calls.createSession.length, 1)
    assert.equal(env.stubs.calls.submitQuery.length, 0, '导航异常不提问')
    const sessionId = chat.currentSessionId.value
    assert.equal(chat.hasCurrentSession(sessionId), true, '已创建会话保留')
    assert.equal(chat.questionDraft.value, '导航抛错的问题')
    assert.ok(chat.askNotice.value.includes('页面切换失败'), '写始终可见的 askNotice')
    assert.equal(chat.creatingSession.value, false)

    await chat.sendQuestion('导航抛错的问题')
    assert.equal(env.stubs.calls.createSession.length, 1, '重试不重复创建')
    assert.equal(ops.replace.length, 2)
    assert.equal(env.stubs.calls.submitQuery.length, 1)
  } finally {
    t.unmount()
  }
})

test('R1-02b 过期流程的 replace 抛异常：不写提示不提问，不影响新页面', async () => {
  const t = await createMounted()
  try {
    const { chat, env } = t
    await readyOn(env, chat)
    const originalReplace = t.router.replace.bind(t.router)
    const navGate = deferred()
    t.router.replace = async () => {
      await navGate.promise
      throw new Error('late navigation guard error')
    }
    const pending = chat.sendQuestion('过期抛错的问题')
    await flush(2)
    // 流程过期：同页预选变化作废
    chat.invalidatePendingCreate()
    assert.equal(chat.creatingSession.value, false)
    navGate.resolve()
    await pending
    await flush()
    assert.equal(chat.askNotice.value, '', '过期 reject 不写提示')
    assert.equal(env.stubs.calls.submitQuery.length, 0)
    t.router.replace = originalReplace
  } finally {
    t.unmount()
  }
})

// ------------------------------------------------------------ Kimi R1 审核回归

test('K11-R1-01 同会话刷新保留草稿：重读详情与历史，草稿原样保留', async () => {
  const t = await createMounted({
    initial: { name: 'nl2sql-query-session', params: { sessionId: 's_live' }, query: {} },
  })
  try {
    const { chat, env } = t
    await chat.openSession('s_live')
    await flush()
    chat.questionDraft.value = '尚未发送的第二问草稿'

    // 与 QueryView.refreshCurrent 相同路径：非忙碌下对同一会话 openSession
    await chat.openSession(chat.currentSessionId.value)
    await flush()
    assert.equal(chat.currentSessionId.value, 's_live')
    assert.equal(chat.questionDraft.value, '尚未发送的第二问草稿', '同会话刷新保留未发送草稿')
    assert.ok(env.stubs.calls.getSession.length >= 2, '刷新仍实际重读详情')
    assert.ok(env.stubs.calls.listTurns.length >= 2, '刷新仍实际重读历史')

    // 失败刷新同样保留草稿
    env.stubs.handlers.getSession = () => Promise.reject(new Nl2sqlApiError('internal_error', '读取失败', 500))
    await chat.openSession('s_live')
    await flush()
    assert.equal(chat.questionDraft.value, '尚未发送的第二问草稿', '失败刷新保留草稿')
    assert.ok(chat.sessionLoadError.value !== '', '失败状态如实展示')
  } finally {
    t.unmount()
  }
})

test('K11-R1-01 边界：切换到其他会话与真正新建仍清空草稿', async () => {
  const t = await createMounted({
    initial: { name: 'nl2sql-query-session', params: { sessionId: 's_live' }, query: {} },
  })
  try {
    const { chat, env } = t
    await chat.openSession('s_live')
    await flush()
    chat.questionDraft.value = '会被清掉的草稿'

    await chat.openSession('s_other')
    await flush()
    assert.equal(chat.currentSessionId.value, 's_other')
    assert.equal(chat.questionDraft.value, '', '切换到其他会话清空草稿')

    chat.questionDraft.value = '新建前草稿'
    chat.resetCurrentSession()
    assert.equal(chat.questionDraft.value, '', '真正新建（resetCurrentSession）清空草稿')
    assert.equal(env.stubs.calls.createSession.length, 0)
  } finally {
    t.unmount()
  }
})

// ---------------------------------------------------------------- doc 21 顶部沿用

test('doc21 沿用：adoptSourceForNewQuery 直接就绪，零页面检查调用、不创建会话', async () => {
  const t = await createMounted()
  try {
    const { chat, env } = t
    chat.adoptSourceForNewQuery(SRC_A)
    await flush()
    assert.equal(chat.selectedSourceId.value, SRC_A)
    assert.equal(chat.readinessState.value, 'ready')
    assert.equal(chat.readinessData.value, null, '沿用不得伪造 ReadinessData')
    assert.equal(chat.sourceInherited.value, true)
    assert.equal(chat.canCompose.value, true, '沿用后必须立即允许输入')
    assert.equal(env.stubs.calls.getReadiness.length, 0, '沿用分支不得调用页面 readiness 接口')
    assert.equal(env.stubs.calls.createSession.length, 0)
    assert.equal(env.stubs.calls.submitQuery.length, 0)
  } finally {
    t.unmount()
  }
})

test('doc21 沿用后手动改选：清除沿用标记，对新库按现有流程检查', async () => {
  const t = await createMounted()
  try {
    const { chat, env } = t
    chat.adoptSourceForNewQuery(SRC_A)
    await flush()
    await readyOn(env, chat, SRC_B)
    assert.equal(chat.sourceInherited.value, false, '手动检查开始即清除沿用标记')
    assert.equal(chat.selectedSourceId.value, SRC_B)
    assert.equal(chat.readinessState.value, 'ready')
    assert.deepEqual(env.stubs.calls.getReadiness, [SRC_B], '只对手动选择的新库发起一次检查')
  } finally {
    t.unmount()
  }
})

test('doc21 旧检查响应晚到：不覆盖沿用数据库，也不写入旧检查结果', async () => {
  const t = await createMounted()
  try {
    const { chat, env } = t
    const gate = deferred()
    env.stubs.handlers.getReadiness = () => gate.promise
    chat.checkReadiness(SRC_B)
    await flush(2)
    assert.equal(chat.readinessState.value, 'checking')

    chat.adoptSourceForNewQuery(SRC_A)
    gate.resolve({ source_id: SRC_B, compatible: true, ready: false, checked_at: 'late', checks: [] })
    await flush()
    assert.equal(chat.selectedSourceId.value, SRC_A, '迟到响应不得改回旧库')
    assert.equal(chat.readinessState.value, 'ready', '迟到响应不得把沿用状态改成 blocked')
    assert.equal(chat.sourceInherited.value, true)
    assert.equal(chat.readinessData.value, null, '迟到响应不得写入伪造的检查结果')
  } finally {
    t.unmount()
  }
})

test('doc21 沿用后首次发送：用沿用库创建一次新会话并在新会话提交', async () => {
  const t = await createMounted({
    initial: { name: 'nl2sql-query', params: {}, query: {} },
  })
  try {
    const { chat, env, ops } = t
    chat.adoptSourceForNewQuery(SRC_A)
    await flush()
    chat.questionDraft.value = '统计上个月的销售额'
    await chat.sendQuestion('统计上个月的销售额')
    await flush()
    assert.equal(env.stubs.calls.getReadiness.length, 0, '沿用首次发送仍不触发页面检查')
    assert.equal(env.stubs.calls.createSession.length, 1, '只创建一次会话')
    assert.equal(env.stubs.calls.createSession[0], SRC_A, '使用沿用的 Orders 库创建')
    assert.equal(ops.replace.length, 1)
    assert.equal(ops.replace[0].name, 'nl2sql-query-session')
    const sessionId = chat.currentSessionId.value
    assert.notEqual(sessionId, '')
    assert.equal(chat.hasCurrentSession(sessionId), true)
    assert.equal(env.stubs.calls.submitQuery.length, 1)
    assert.equal(env.stubs.calls.submitQuery[0][0], sessionId, '提交到新会话，不是旧会话')
    assert.equal(env.stubs.calls.submitQuery[0][2], SRC_A)
    assert.equal(chat.questionDraft.value, '', '提交成功清空草稿')
  } finally {
    t.unmount()
  }
})
