/**
 * doc 21 新建查询入口接线测试。
 *
 * 复用现有 .cjs 测试的真实挂载方式：compiler-sfc 编译真实 QueryView.vue /
 * AppSidebar.vue，真实 Vue createRenderer 挂载，真实 vue-router（memory history）
 * 驱动导航，真实 useChat.ts / newQueryReset.ts / queryBusy.ts 参与接线，
 * 只替换 API 边界与展示型子组件。不复制一份路由监听算法到测试里。
 */
const assert = require('node:assert/strict')
const { test } = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')
const vueRouter = require('vue-router')
const { parse, compileScript } = require('@vue/compiler-sfc')

const NL2SQL_DIR = __dirname
const FRONTEND_SRC = path.resolve(NL2SQL_DIR, '../..')
const SRC_A = 'db_src_1'
const SRC_B = 'db_src_2'

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
class Nl2sqlNotConfiguredError extends Error {}
class ApiError extends Error {}

function makeSession(sourceId, overrides = {}) {
  return {
    session_id: 's_stub',
    source_id: sourceId,
    title: '历史对话',
    state: 'active',
    continuable: true,
    created_at: '2026-11-16T07:00:00+08:00',
    last_active_at: '2026-11-16T07:00:00+08:00',
    turn_count: 0,
    invalid_reason: null,
    invalidated_at: null,
    ...overrides,
  }
}

function makeResult() {
  return {
    status: 'completed',
    answer: 'ok',
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
  const calls = { listDataSources: [], getReadiness: [], createSession: [], getSession: [], listTurns: [], submitQuery: [] }
  const handlers = {}
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
      return Promise.resolve(makeSession(sourceId, { session_id: `s_${++sessionSeq}`, title: '新对话' }))
    },
    getSession(sessionId) {
      calls.getSession.push(sessionId)
      return handlers.getSession
        ? handlers.getSession(sessionId, calls.getSession.length)
        : Promise.resolve(makeSession(SRC_A, { session_id: sessionId }))
    },
    listTurns(sessionId, limit, offset) {
      calls.listTurns.push([sessionId, limit, offset])
      return Promise.resolve({ items: [], has_more: false, next_offset: null, offset })
    },
    submitQuery(sessionId, question, sourceId) {
      calls.submitQuery.push([sessionId, question, sourceId])
      return Promise.resolve(makeResult())
    },
  }
  const dataSources = {
    listDataSources() {
      calls.listDataSources.push(1)
      return Promise.resolve({
        items: [
          { source_id: SRC_A, display_name: 'Orders订单库', db_type: 'postgresql', db_name: 'orders', selected_schemas: ['public'], is_active: true },
          { source_id: SRC_B, display_name: 'B库', db_type: 'postgresql', db_name: 'b', selected_schemas: ['public'], is_active: true },
        ],
      })
    },
  }
  return { api, dataSources, calls, handlers }
}

// ---------------------------------------------------------------- 展示型子组件替身

function displayStub(name, propNames, render) {
  return {
    name,
    props: propNames,
    setup(props) {
      return () => vue.h('div', { class: `stub-${name}` }, render(props))
    },
  }
}

const SourceSelectorStub = displayStub(
  'SourceSelector',
  ['sources', 'loading', 'error', 'selectedSourceId', 'readinessState', 'readinessData', 'readinessError', 'busy', 'notice', 'sourceInherited'],
  (p) => `selected:${p.selectedSourceId || ''}|inherited:${p.sourceInherited ? 1 : 0}|state:${p.readinessState}`,
)
const QuestionInputStub = displayStub(
  'QuestionInput',
  ['modelValue', 'disabled', 'blockedReason', 'submitting', 'creating', 'elapsedSeconds', 'sourceLabel'],
  (p) => `blocked:${p.blockedReason || ''}|disabled:${p.disabled ? 1 : 0}`,
)
const MessageListStub = displayStub('MessageList', [], () => 'messages')
const HistoryDialogStub = displayStub('ConversationHistoryDialog', [], () => 'history')

// ---------------------------------------------------------------- 模块加载

const STUB_FILES = new Set([
  'modules/nl2sql/api/client',
  'modules/nl2sql/api/types',
  'api/dataSources',
  'api/http',
  'api/types',
])

function createEnv(options = {}) {
  const stubs = createApiStub()
  const mountQueue = []
  const chatHandles = []
  let vueForModules = vue
  if (options.deferMount) {
    vueForModules = { ...vue }
    vueForModules.onMounted = (fn) => {
      mountQueue.push(fn)
    }
  }
  const cache = new Map()
  const contextGlobals = {
    console,
    __APP_BUILD_INFO__: { version: '123abcd', builtAt: '2026-11-18 02:30' },
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    URL,
    URLSearchParams,
    window: { setTimeout, clearTimeout, setInterval, clearInterval },
  }

  function stubModule(key) {
    if (key === 'modules/nl2sql/api/client') return stubs.api
    if (key === 'api/dataSources') return stubs.dataSources
    if (key === 'api/http') return { ApiError }
    return {}
  }

  function keyOf(abs) {
    return path.relative(FRONTEND_SRC, abs).replaceAll('\\', '/').replace(/\.ts$/, '')
  }

  function loadFile(absNoExt) {
    const key = keyOf(absNoExt)
    if (cache.has(key)) return cache.get(key)
    if (STUB_FILES.has(key)) {
      const module = { exports: stubModule(key) }
      cache.set(key, module.exports)
      return module.exports
    }
    for (const candidate of [absNoExt, absNoExt + '.ts', absNoExt + '/index.ts']) {
      if (!fs.existsSync(candidate) || !fs.statSync(candidate).isFile()) continue
      const source = fs.readFileSync(candidate, 'utf8')
      const js = ts.transpileModule(source, {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
      }).outputText
      const module = { exports: {} }
      cache.set(key, module.exports)
      const run = new vm.Script(`(function (exports, require, module) {\n${js}\n})`, { filename: candidate })
      run.runInContext(vm.createContext(contextGlobals))(
        module.exports,
        (specifier) => resolveModule(specifier, path.dirname(candidate)),
        module,
      )
      cache.set(key, module.exports)
      return module.exports
    }
    throw new Error(`无法加载模块：${key}`)
  }

  const formatModule = loadFile(path.join(NL2SQL_DIR, 'format'))
  const queryHomeLinkModule = loadFile(path.join(NL2SQL_DIR, 'queryHomeLink'))
  const queryBusyModule = loadFile(path.join(NL2SQL_DIR, 'queryBusy'))
  const useChatModule = loadFile(path.join(NL2SQL_DIR, 'composables/useChat'))

  // 包装真实 composable，仅用于把返回值暴露给断言，不替换任何协调逻辑。
  const useChatWrapped = {
    ...useChatModule,
    useNl2sqlChat(router) {
      const chat = useChatModule.useNl2sqlChat(router)
      chatHandles.push(chat)
      return chat
    },
  }

  function loadNewQueryReset() {
    return loadFile(path.join(NL2SQL_DIR, 'newQueryReset'))
  }

  function resolveModule(specifier, fromDir) {
    if (specifier === 'vue') return vueForModules
    if (specifier === 'vue-router') return vueRouter
    if (specifier.startsWith('.')) {
      const abs = path.resolve(fromDir, specifier)
      const key = keyOf(abs)
      if (STUB_FILES.has(key)) {
        if (!cache.has(key)) cache.set(key, stubModule(key))
        return cache.get(key)
      }
      return loadFile(abs)
    }
    throw new Error(`未替换的运行时依赖：${specifier}`)
  }

  function resolveSfcSpecifier(specifier) {
    if (specifier === 'vue') return vueForModules
    if (specifier === 'vue-router') return vueRouter
    if (specifier.includes('api/client')) return stubs.api
    if (specifier.includes('api/types')) return {}
    if (specifier.includes('composables/useChat')) return useChatWrapped
    if (specifier.includes('queryHomeLink')) return queryHomeLinkModule
    if (specifier.includes('queryBusy')) return queryBusyModule
    if (specifier.includes('newQueryReset')) return loadNewQueryReset()
    if (specifier.includes('/format')) return formatModule
    if (specifier.includes('ConversationHistoryDialog')) return { default: HistoryDialogStub }
    if (specifier.includes('MessageList')) return { default: MessageListStub }
    if (specifier.includes('QuestionInput')) return { default: QuestionInputStub }
    if (specifier.includes('SourceSelector')) return { default: SourceSelectorStub }
    throw new Error(`SFC 未替换依赖：${specifier}`)
  }

  function loadEsModule(code, filename, resolve) {
    const js = ts.transpileModule(code, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    }).outputText
    const module = { exports: {} }
    const run = new vm.Script(`(function (exports, require, module) {\n${js}\n})`, { filename })
    run.runInContext(vm.createContext(contextGlobals))(module.exports, resolve, module)
    return module.exports.default
  }

  function loadSfc(filename, id) {
    const source = fs.readFileSync(filename, 'utf8')
    const { descriptor, errors } = parse(source, { filename })
    assert.equal(errors.length, 0, `${path.basename(filename)} SFC 解析失败`)
    const script = compileScript(descriptor, { id, inlineTemplate: true })
    return loadEsModule(script.content, `${id}.mjs`, resolveSfcSpecifier)
  }

  const QueryView = loadSfc(path.join(NL2SQL_DIR, 'views/QueryView.vue'), 'qv-nav-test')
  const AppSidebar = loadSfc(path.join(FRONTEND_SRC, 'components/AppSidebar.vue'), 'sb-nav-test')

  async function runMounted() {
    const queued = mountQueue.splice(0)
    for (const fn of queued) await fn()
  }

  return {
    stubs,
    vueForModules,
    QueryView,
    AppSidebar,
    queryBusyModule,
    loadNewQueryReset,
    chatHandles,
    runMounted,
  }
}

// ---------------------------------------------------------------- Vue 宿主渲染器

class FakeElement extends EventTarget {
  constructor(tag) {
    super()
    this.tag = tag
    this.children = []
    this.parentNode = null
    this.attributes = {}
    this.value = ''
    this.disabled = false
    this.className = ''
  }
  setAttribute(key, value) {
    this.attributes[key] = String(value)
  }
  getAttribute(key) {
    return key in this.attributes ? this.attributes[key] : null
  }
  removeAttribute(key) {
    delete this.attributes[key]
  }
}

function createHostRenderer() {
  const nodeOps = {
    insert(child, parent, anchor) {
      child.parentNode = parent
      parent.children = parent.children || []
      const index = anchor ? parent.children.indexOf(anchor) : -1
      if (index === -1) parent.children.push(child)
      else parent.children.splice(index, 0, child)
    },
    remove(child) {
      const parent = child.parentNode
      if (parent?.children) parent.children.splice(parent.children.indexOf(child), 1)
      child.parentNode = null
    },
    createElement: (tag) => new FakeElement(tag),
    createText: (text) => ({ text, parentNode: null, nodeType: 3 }),
    createComment: (text) => ({ text: '', comment: text, parentNode: null }),
    setText(node, text) {
      node.text = text
    },
    setElementText(el, text) {
      el.children = [{ text, nodeType: 3 }]
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
  const patchProp = (el, key, prev, next) => {
    if (key === 'class') {
      el.className = next ?? ''
      return
    }
    if (key === 'disabled') {
      el.disabled = next === true
      return
    }
    if (key === 'value') {
      el.value = next ?? ''
      return
    }
    if (key.startsWith('on')) {
      const type = key.slice(2).toLowerCase()
      // RouterLink 非 custom 时，组件 attrs 的 onClick 会与内部 navigate 合并成数组；
      // 宿主需按数组逐个注册，否则点击不会触发导航（真实 DOM 由 runtime-dom 处理）。
      const asArray = (value) => (Array.isArray(value) ? value : value ? [value] : [])
      for (const fn of asArray(prev)) el.removeEventListener(type, fn)
      for (const fn of asArray(next)) el.addEventListener(type, fn)
      return
    }
    if (next == null) el.removeAttribute(key)
    else el.setAttribute(key, next)
  }
  return vue.createRenderer({ ...nodeOps, patchProp })
}

const renderer = createHostRenderer()

function walk(root, fn) {
  if (!root) return
  fn(root)
  for (const child of root.children || []) walk(child, fn)
}

function textOf(root) {
  const parts = []
  walk(root, (node) => {
    if (node.text !== undefined) parts.push(node.text)
  })
  return parts.join('')
}

function findAll(root, predicate) {
  const found = []
  walk(root, (node) => {
    if (predicate(node)) found.push(node)
  })
  return found
}

function hasClass(node, name) {
  return typeof node.className === 'string' && node.className.includes(name)
}

function click(el, extra = {}) {
  const event = new Event('click', { cancelable: true, bubbles: true })
  for (const [key, value] of Object.entries(extra)) {
    Object.defineProperty(event, key, { value })
  }
  el.dispatchEvent(event)
  return event
}

const flush = async (rounds = 16) => {
  for (let i = 0; i < rounds; i++) await new Promise((resolve) => setImmediate(resolve))
}

async function waitFor(predicate, label = '条件', rounds = 50) {
  for (let i = 0; i < rounds; i++) {
    if (predicate()) return
    await vue.nextTick()
    await new Promise((resolve) => setImmediate(resolve))
  }
  throw new Error(`等待超时：${label}`)
}

// ---------------------------------------------------------------- 测试宿主

async function createHarness(options = {}) {
  const env = createEnv(options)
  if (options.setupStubs) options.setupStubs(env.stubs)
  const flags = { blockInherited: false }
  const router = vueRouter.createRouter({
    history: vueRouter.createMemoryHistory(),
    routes: [
      { path: '/query', name: 'nl2sql-home', component: displayStub('Home', [], () => 'home') },
      { path: '/query/new', name: 'nl2sql-query', component: env.QueryView },
      { path: '/query/:sessionId', name: 'nl2sql-query-session', component: env.QueryView },
    ],
  })
  router.beforeEach((to) => {
    if (flags.blockInherited && to.name === 'nl2sql-query' && to.query.source_id) return false
    return true
  })
  await router.push(options.initial || '/query/s_1?home=/query')
  await router.isReady()

  const busy = vue.ref(false)
  const container = { children: [], parentNode: null }
  const Root = {
    setup() {
      return () =>
        vue.h('div', { class: 'root' }, [vue.h(env.AppSidebar), vue.h(vueRouter.RouterView)])
    },
  }
  const app = renderer.createApp(Root)
  app.use(router)
  app.provide(env.queryBusyModule.queryBusyKey, busy)
  app.mount(container)
  await flush()

  const harness = {
    env,
    router,
    app,
    container,
    busy,
    flags,
    get chat() {
      return env.chatHandles[0] || null
    },
    get text() {
      return textOf(container)
    },
    runMounted: env.runMounted,
    async settle() {
      await vue.nextTick()
      await flush()
    },
    async unmount() {
      app.unmount()
      await flush()
    },
  }
  return harness
}

function topNewQueryButton(h) {
  return findAll(h.container, (node) => hasClass(node, 'head-btn-primary'))[0] || null
}

/** 沿用标记在实现前不存在，读取时按 false 处理，使回归类断言只针对行为。 */
function inherited(h) {
  return h.chat.sourceInherited ? h.chat.sourceInherited.value : false
}

/**
 * doc21 R1-01：只取真实 .new-guide 区域的文本，不用整页文本（页头本来就含沿用说明）。
 * 宿主 insertStaticContent() 是空替身，静态提升的文案不会被插入；目标标题/描述/
 * 第 2 步改为插值后是动态文本，可正常观察。调用方须先确认区域可观察，
 * 再用正向断言证明标题/描述确实进入该区域，避免「静态文案被丢弃」造成假阴性。
 */
function guideText(h) {
  const guide = findAll(h.container, (node) => hasClass(node, 'new-guide'))[0]
  return guide ? textOf(guide) : ''
}

function assertGuideObservable(text, label) {
  assert.ok(
    text.includes('选择数据库') && text.includes('输入自然语言问题'),
    `${label}：测试宿主须能观察 .new-guide 区域文本（当前=${JSON.stringify(text)}）`,
  )
}

function findSidebarNewQueryLink(h) {
  return (
    findAll(
      h.container,
      (node) =>
        hasClass(node, 'query-sub') &&
        textOf(node).trim() === '新建查询' &&
        node.getAttribute('href') !== null,
    )[0] || null
  )
}

async function sidebarNewQueryLink(h) {
  const existing = findSidebarNewQueryLink(h)
  if (existing) return existing
  const chevron = findAll(
    h.container,
    (node) => node.attributes?.['aria-label'] === '展开或收起智能问数子菜单',
  )[0]
  if (chevron) {
    click(chevron)
    await h.settle()
  }
  return findSidebarNewQueryLink(h)
}

// ---------------------------------------------------------------- 用例

test('导航栏底部展示构建版本和北京时间', async () => {
  const h = await createHarness({ initial: '/query' })
  try {
    const footer = findAll(h.container, (node) => node.tag === 'footer')[0]
    assert.ok(footer)
    const text = textOf(footer)
    assert.ok(text.includes('版本：123abcd'))
    assert.ok(text.includes('构建时间：2026-11-18 02:30'))
    assert.ok(text.includes('（北京时间）'))
  } finally {
    await h.unmount()
  }
})

test('doc21 顶部沿用：会话页显示顶部按钮，点击后预选当前库、零页面检查、立即允许输入', async () => {
  const h = await createHarness({ initial: '/query/s_1?home=/query' })
  try {
    await waitFor(() => h.chat?.sessionInfo.value !== null, '会话详情加载')
    assert.ok(topNewQueryButton(h), '会话页必须渲染顶部蓝色新建查询按钮')

    click(topNewQueryButton(h))
    await h.settle()

    const current = h.router.currentRoute.value
    assert.equal(current.name, 'nl2sql-query', '应落在新建页')
    assert.equal(current.query.source_id, SRC_A, '地址携带沿用数据库')
    assert.equal(h.env.stubs.calls.getReadiness.length, 0, '页面 readiness 调用增量为 0')
    assert.equal(h.chat.selectedSourceId.value, SRC_A)
    assert.equal(h.chat.readinessState.value, 'ready')
    assert.equal(inherited(h), true)
    assert.equal(h.chat.canCompose.value, true, '沿用后立即允许输入')
    assert.ok(h.text.includes('inherited:1'), 'SourceSelector 收到沿用标记')
    assert.ok(h.text.includes('已沿用当前数据库，可直接输入问题。'), '页头说明与可输入状态一致')
    assert.ok(
      h.text.includes('blocked:|disabled:0'),
      `提问框不再被禁用：len=${h.text.length} tail=${h.text.slice(-300)}`,
    )
    assert.equal(topNewQueryButton(h), null, '新建页不渲染顶部沿用按钮')

    // doc21 R1-01：断言落在 .new-guide 区域本身，不以页头包含沿用说明代替。
    const guide = guideText(h)
    assertGuideObservable(guide, '沿用场景')
    assert.ok(guide.includes('开始智能查询'), '沿用标题不再要求选择数据库')
    assert.ok(!guide.includes('选择数据库，开始智能查询'), '沿用标题已替换原提示')
    assert.ok(guide.includes('已沿用当前数据库，可直接输入问题。'), '沿用描述进入空状态')
    assert.ok(!guide.includes('选择数据库并完成检查后'), '沿用描述不再要求完成检查')
    assert.ok(guide.includes('沿用数据库'), '第 2 步改为沿用数据库')
    assert.ok(!guide.includes('自动完成检查'), '第 2 步不再称自动完成检查')
  } finally {
    await h.unmount()
  }
})

test('doc21 R1-01：普通新建页空状态保留原提示，仅沿用场景局部切换文案', async () => {
  const h = await createHarness({ initial: '/query/new' })
  try {
    await h.settle()
    assert.equal(inherited(h), false, '普通新建页无沿用标记')
    const guide = guideText(h)
    assertGuideObservable(guide, '普通新建页')
    assert.ok(guide.includes('选择数据库，开始智能查询'), '普通新建页保留原标题')
    assert.ok(
      guide.includes('选择数据库并完成检查后，用自然语言开始提问。'),
      '普通新建页保留原描述（同时证明该区域标题/描述文本可被宿主观察）',
    )
    assert.ok(guide.includes('自动完成检查'), '普通新建页保留原第 2 步')
    assert.ok(!guide.includes('沿用数据库'), '普通新建页不出现沿用第 2 步文案')
    assert.ok(!guide.includes('已沿用当前数据库'), '普通新建页不出现沿用描述')
  } finally {
    await h.unmount()
  }
})

test('doc21 当前会话信息尚未加载：不猜测数据库，进入普通新建页', async () => {
  const h = await createHarness({
    initial: '/query/s_pending?home=/query',
    setupStubs: (stubs) => {
      stubs.handlers.getSession = () => new Promise(() => {})
    },
  })
  try {
    assert.equal(h.chat.sessionInfo.value, null, '会话详情尚未加载')
    assert.ok(topNewQueryButton(h))
    click(topNewQueryButton(h))
    await h.settle()
    assert.equal(h.router.currentRoute.value.query.source_id, undefined, '不猜测数据库')
    assert.equal(h.chat.selectedSourceId.value, '')
    assert.equal(inherited(h), false)
    assert.equal(h.env.stubs.calls.getReadiness.length, 0)
  } finally {
    await h.unmount()
  }
})

test('doc21 左侧入口：任意页面点击后地址不带数据库参数、页面未选库', async () => {
  const h = await createHarness({ initial: '/query/s_1?home=/query' })
  try {
    await waitFor(() => h.chat?.sessionInfo.value !== null, '会话详情加载')
    const link = await sidebarNewQueryLink(h)
    assert.ok(link, '侧栏新建查询链接可点击')
    click(link)
    await h.settle()
    const current = h.router.currentRoute.value
    assert.equal(current.name, 'nl2sql-query')
    assert.equal(current.query.source_id, undefined, '左侧入口不携带 source_id')
    assert.equal(h.env.stubs.calls.getReadiness.length, 0, '未选库不触发页面检查')
    assert.equal(h.chat.selectedSourceId.value, '')
    assert.equal(h.chat.readinessState.value, 'idle')
  } finally {
    await h.unmount()
  }
})

test('doc21 从带 source_id 的新建页点击左侧入口：同步清空并去掉地址参数，刷新不恢复', async () => {
  const h = await createHarness({ initial: `/query/new?source_id=${SRC_A}&home=/query` })
  try {
    await waitFor(() => h.chat?.selectedSourceId.value === SRC_A, '普通预选检查完成')
    const before = h.env.stubs.calls.getReadiness.length
    assert.ok(before >= 1, '普通预选页按现有流程自动检查')

    const link = await sidebarNewQueryLink(h)
    click(link)
    await h.settle()
    const current = h.router.currentRoute.value
    assert.equal(current.query.source_id, undefined, '导航完成后地址不含 source_id')
    assert.equal(current.query.home, undefined, '左侧入口不保留其他查询参数')
    assert.equal(h.chat.selectedSourceId.value, '', '同步通知清空选择')
    assert.equal(inherited(h), false)
    assert.equal(h.env.stubs.calls.getReadiness.length, before, '不新增该库的页面检查')

    // 刷新等价路径：用落地后的无参地址新建宿主，不应恢复预选或触发检查
    const fresh = await createHarness({ initial: '/query/new' })
    try {
      await fresh.settle()
      assert.equal(fresh.chat.selectedSourceId.value, '')
      assert.equal(fresh.env.stubs.calls.getReadiness.length, 0, '刷新不恢复预选、不触发检查')
    } finally {
      await fresh.unmount()
    }
  } finally {
    await h.unmount()
  }
})

test('doc21 已选库的新建页再次点击左侧入口：重复导航也清空选库、草稿及沿用标记', async () => {
  // 地址已是 /query/new，手动选库后再次点击左侧入口属于 Vue Router 重复导航：
  // 没有任何监听会因路由变化而触发，清空必须由同步重置通知完成。
  const h = await createHarness({ initial: '/query/new' })
  try {
    h.chat.checkReadiness(SRC_A)
    await h.settle()
    assert.equal(h.chat.selectedSourceId.value, SRC_A, '已手动选库')
    h.chat.questionDraft.value = '未发送草稿'
    const link = await sidebarNewQueryLink(h)
    click(link)
    await h.settle()
    assert.equal(h.router.currentRoute.value.fullPath, '/query/new', '路由重复，地址不变')
    assert.equal(h.chat.selectedSourceId.value, '', '清空选库')
    assert.equal(h.chat.questionDraft.value, '', '清空未发送草稿')
    assert.equal(h.chat.readinessState.value, 'idle')
    assert.equal(inherited(h), false, '清除沿用标记')
  } finally {
    await h.unmount()
  }
})

test('doc21 顶部导航失败后改用左侧入口：不残留沿用意图、不错误恢复数据库', async () => {
  const h = await createHarness({ initial: '/query/s_1?home=/query' })
  try {
    await waitFor(() => h.chat?.sessionInfo.value !== null, '会话详情加载')
    h.flags.blockInherited = true
    click(topNewQueryButton(h))
    await h.settle()
    assert.equal(h.router.currentRoute.value.name, 'nl2sql-query-session', '导航失败仍停留在会话页')

    h.flags.blockInherited = false
    const link = await sidebarNewQueryLink(h)
    click(link)
    await h.settle()
    assert.equal(h.router.currentRoute.value.query.source_id, undefined)
    assert.equal(h.chat.selectedSourceId.value, '', '失配意图不得恢复数据库')
    assert.equal(inherited(h), false)
    assert.equal(h.env.stubs.calls.getReadiness.length, 0)
  } finally {
    await h.unmount()
  }
})

test('doc21 顶部沿用时 apiReady 延迟：就绪前不消费、不检查，就绪后应用一次且不被旧会话重置覆盖', async () => {
  const h = await createHarness({ initial: '/query/s_1?home=/query', deferMount: true })
  try {
    await waitFor(() => h.chat?.sessionInfo.value !== null, '会话详情加载')
    click(topNewQueryButton(h))
    await h.settle()
    assert.equal(h.router.currentRoute.value.name, 'nl2sql-query', '已落地新建页')
    assert.equal(h.chat.selectedSourceId.value, '', 'apiReady 未就绪时不消费意图')
    assert.equal(h.env.stubs.calls.getReadiness.length, 0, '就绪前不发起页面检查')

    await h.runMounted()
    await h.settle()
    assert.equal(h.chat.selectedSourceId.value, SRC_A, '就绪后由预选监听应用沿用')
    assert.equal(inherited(h), true)
    assert.equal(h.chat.canCompose.value, true)
    assert.equal(h.env.stubs.calls.getReadiness.length, 0, '仍为零页面检查')
    assert.equal(h.chat.currentSessionId.value, '', '旧会话已被重置，沿用状态未被其覆盖')
  } finally {
    await h.unmount()
  }
})

test('doc21 修饰键点击左侧入口：保留链接行为，不清空原标签页选择或草稿', async () => {
  const h = await createHarness({ initial: `/query/new?source_id=${SRC_A}` })
  try {
    await waitFor(() => h.chat?.selectedSourceId.value === SRC_A, '预选就绪')
    h.chat.questionDraft.value = '保留的草稿'
    const link = await sidebarNewQueryLink(h)
    click(link, { metaKey: true })
    await h.settle()
    assert.equal(h.router.currentRoute.value.query.source_id, SRC_A, 'meta 点击不触发站内导航')
    assert.equal(h.chat.selectedSourceId.value, SRC_A, '不清空原标签页选择')
    assert.equal(h.chat.questionDraft.value, '保留的草稿', '不清空原标签页草稿')
  } finally {
    await h.unmount()
  }
})

test('doc21 忙碌期间两个入口保持既有忙碌限制', async () => {
  const h = await createHarness({ initial: '/query/s_1?home=/query' })
  try {
    await waitFor(() => h.chat?.sessionInfo.value !== null, '会话详情加载')
    h.chat.creatingSession.value = true
    await h.settle()
    assert.equal(h.busy.value, true, '共享忙碌标记已同步')

    const before = h.router.currentRoute.value.fullPath
    click(topNewQueryButton(h))
    await h.settle()
    assert.equal(h.router.currentRoute.value.fullPath, before, '忙碌时顶部入口不导航')

    const link = await sidebarNewQueryLink(h)
    click(link)
    await h.settle()
    assert.equal(h.router.currentRoute.value.fullPath, before, '忙碌时左侧入口不导航')
  } finally {
    h.chat.creatingSession.value = false
    await h.unmount()
  }
})

test('doc21 同步重置通知：无接收者直接结束，旧实例注销不误清新实例注册', async () => {
  const mod = createEnv().loadNewQueryReset()
  let a = 0
  let b = 0
  const offA = mod.registerNewQueryReset(() => {
    a += 1
  })
  mod.notifyNewQueryReset()
  assert.equal(a, 1)

  const offB = mod.registerNewQueryReset(() => {
    b += 1
  })
  offA()
  mod.notifyNewQueryReset()
  assert.equal(b, 1, '旧实例注销不得清除新实例注册')
  assert.equal(a, 1, '被替换的回调不再收到通知')

  offB()
  mod.notifyNewQueryReset()
  assert.equal(a, 1)
  assert.equal(b, 1, '无接收者时不排队、不重放')
})
