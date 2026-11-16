/**
 * SourceSelector 组件测试（doc11 / Kimi R1-02）：实际 SFC 经 compiler-sfc 编译、
 * 真实 Vue createRenderer 挂载——数据源列表错误与创建提示必须互不遮挡、独立可见。
 * 复用 QuestionInput.test.cjs 的宿主方式，不新增依赖。
 */
const assert = require('node:assert/strict')
const { test } = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')
const { parse, compileScript } = require('@vue/compiler-sfc')

const COMPONENT_PATH = path.join(__dirname, 'SourceSelector.vue')

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
      else parent.children.splice(index, 1, child)
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
      if (prev) el.removeEventListener(type, prev)
      if (next) el.addEventListener(type, next)
      return
    }
    if (next == null) el.removeAttribute(key)
    else el.setAttribute(key, next)
  }
  return vue.createRenderer({ ...nodeOps, patchProp })
}

const renderer = createHostRenderer()

function loadEsModule(code, filename, resolve) {
  const js = ts.transpileModule(code, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText
  const module = { exports: {} }
  const run = new vm.Script(`(function (exports, require, module) {\n${js}\n})`, { filename })
  run.runInContext(vm.createContext({ console }))(module.exports, resolve, module)
  return module.exports
}

let compiled = null
function compileSourceSelector() {
  if (compiled) return compiled
  const source = fs.readFileSync(COMPONENT_PATH, 'utf8')
  const { descriptor, errors } = parse(source, { filename: COMPONENT_PATH })
  assert.equal(errors.length, 0, 'SFC 解析失败')
  const script = compileScript(descriptor, { id: 'ss-test', inlineTemplate: true })
  compiled = loadEsModule(script.content, 'SourceSelector-script.mjs', (specifier) => {
    if (specifier === 'vue') return vue
    if (specifier === '../../../api/types') return {}
    if (specifier === '../api/types') return {}
    if (specifier === '../composables/useChat') return {}
    throw new Error(`未替换依赖：${specifier}`)
  }).default
  return compiled
}

const SOURCE = {
  source_id: 'db_src_1',
  display_name: 'Orders订单库',
  db_type: 'postgresql',
  db_name: 'orders',
  selected_schemas: ['public'],
  is_active: true,
}

function textOf(root) {
  const parts = []
  const walk = (node) => {
    if (!node) return
    if (node.text !== undefined) parts.push(node.text)
    for (const child of node.children || []) walk(child)
  }
  walk(root)
  return parts.join('')
}

// doc 21 §6.1：普通场景显式提供 sourceInherited: false；沿用场景覆盖为 true。
function mountSelector(props) {
  const container = { children: [], parentNode: null }
  const app = renderer.createApp({
    setup() {
      return () => vue.h(compileSourceSelector(), { sourceInherited: false, ...props })
    },
  })
  app.mount(container)
  return { container, app, text: textOf(container) }
}

/** 沿用标记可变的宿主：用于验证标记清除后主文案同步回到普通就绪文案。 */
function mountSelectorLive(props) {
  const container = { children: [], parentNode: null }
  const inherited = vue.ref(props.sourceInherited ?? false)
  const app = renderer.createApp({
    setup() {
      return () =>
        vue.h(compileSourceSelector(), { ...props, sourceInherited: inherited.value })
    },
  })
  app.mount(container)
  return {
    app,
    get text() {
      return textOf(container)
    },
    setInherited(value) {
      inherited.value = value
    },
  }
}

function readyProps(overrides = {}) {
  return {
    sources: [SOURCE],
    loading: false,
    error: '',
    selectedSourceId: 'db_src_1',
    readinessState: 'ready',
    readinessData: null,
    readinessError: '',
    busy: false,
    notice: '',
    sourceInherited: false,
    ...overrides,
  }
}

const PENDING_NOTICE = '会话创建结果尚未确认，请勿重复发送，可稍后从查询历史查看。'

test('列表错误与创建提示并存：创建提示必须可见，不互相遮挡', async () => {
  const pending = mountSelector({
    sources: [SOURCE],
    loading: false,
    error: '数据源列表加载失败，请稍后重试。',
    selectedSourceId: 'db_src_1',
    readinessState: 'ready',
    readinessData: null,
    readinessError: '',
    busy: false,
    notice: PENDING_NOTICE,
  })
  assert.ok(pending.text.includes(PENDING_NOTICE), '超时/网络失败的统一创建提示必须渲染')
  assert.ok(pending.text.includes('数据源列表加载失败'), '真实列表错误仍保留展示，不清除')

  const generic = mountSelector({
    sources: [SOURCE],
    loading: false,
    error: '数据源列表加载失败，请稍后重试。',
    selectedSourceId: 'db_src_1',
    readinessState: 'ready',
    readinessData: null,
    readinessError: '',
    busy: false,
    notice: '数据源暂未就绪，请检查发布状态后重试。',
  })
  assert.ok(generic.text.includes('数据源暂未就绪'), '普通创建错误同样不被列表错误遮挡')
  assert.ok(generic.text.includes('数据源列表加载失败'))

  pending.app.unmount()
  generic.app.unmount()
})

test('单独存在：仅有创建提示或仅有列表错误时各自可见，草稿态提示链不受影响', async () => {
  const onlyNotice = mountSelector({
    sources: [SOURCE],
    loading: false,
    error: '',
    selectedSourceId: 'db_src_1',
    readinessState: 'ready',
    readinessData: null,
    readinessError: '',
    busy: false,
    notice: PENDING_NOTICE,
  })
  assert.ok(onlyNotice.text.includes(PENDING_NOTICE))

  const onlyError = mountSelector({
    sources: [],
    loading: false,
    error: '数据源列表加载失败，请稍后重试。',
    selectedSourceId: '',
    readinessState: 'idle',
    readinessData: null,
    readinessError: '',
    busy: false,
    notice: '',
  })
  assert.ok(onlyError.text.includes('数据源列表加载失败'))
  assert.ok(!onlyError.text.includes(PENDING_NOTICE))

  const idle = mountSelector({
    sources: [SOURCE],
    loading: false,
    error: '',
    selectedSourceId: '',
    readinessState: 'idle',
    readinessData: null,
    readinessError: '',
    busy: false,
    notice: '',
  })
  assert.ok(idle.text.includes('新建查询前先选择数据库'), '无提示时状态链正常')

  onlyNotice.app.unmount()
  onlyError.app.unmount()
  idle.app.unmount()
})

test('doc21 §5.4：沿用就绪与普通检查就绪主文案互斥，不叠加', async () => {
  const inherited = mountSelector(readyProps({ sourceInherited: true }))
  assert.ok(inherited.text.includes('已沿用当前数据库，可直接输入问题。'), '沿用主文案必须展示')
  assert.ok(
    !inherited.text.includes('数据源已就绪，请在下方输入问题。'),
    '沿用场景不得再展示普通检查就绪主文案',
  )

  const normal = mountSelector(readyProps({ sourceInherited: false }))
  assert.ok(normal.text.includes('数据源已就绪，请在下方输入问题。'), '普通检查就绪保留原文案')
  assert.ok(!normal.text.includes('已沿用当前数据库'), '普通检查就绪不得展示沿用主文案')

  inherited.app.unmount()
  normal.app.unmount()
})

test('doc21 §5.4：沿用标记清除后主文案同步回到普通就绪文案', async () => {
  const live = mountSelectorLive(readyProps({ sourceInherited: true }))
  assert.ok(live.text.includes('已沿用当前数据库，可直接输入问题。'))
  live.setInherited(false)
  await vue.nextTick()
  assert.ok(live.text.includes('数据源已就绪，请在下方输入问题。'))
  assert.ok(!live.text.includes('已沿用当前数据库'))
  live.app.unmount()
})
