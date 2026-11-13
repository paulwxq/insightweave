/**
 * WebTurn.open_questions 可执行行为验收（doc 6.3 §8.3；Codex R2 M1）。
 *
 * 与 openQuestionsContract.test.cjs 的「源码契约检查」不同，本文件执行真实
 * useChat.ts::snapshotTurn 与真实 MessageList.vue（compiler-sfc 编译 + Vue
 * createRenderer 挂载），只替换 AnswerCard/format 等展示边界，不复制算法：
 * - 三态：null（旧记录未知）/ []（新记录明确无问题）/ 非空；
 * - 实时快照与历史刷新一致（status 不限于 needs_clarification）；
 * - 安全投影：不安全终态写 []，原问题不泄漏到实时或历史。
 */
const assert = require('node:assert/strict')
const { test } = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')
const { parse, compileScript } = require('@vue/compiler-sfc')

const NL2SQL_DIR = __dirname
const USECHAT_PATH = path.join(NL2SQL_DIR, 'composables/useChat.ts')
const MESSAGELIST_PATH = path.join(NL2SQL_DIR, 'components/MessageList.vue')

// ------------------------------------------------------------- Vue 宿主渲染器

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

// AnswerCard 展示边界替身：把实际投影到的 clarify 列表编码成文本，
// 便于断言「问题是否真的被渲染」，不复制 MessageList 的映射算法。
const AnswerCardStub = {
  props: ['sessionId', 'answer', 'elapsedMs', 'evidence', 'clarify', 'unsafe'],
  setup(props) {
    return () =>
      vue.h('div', { class: 'answer-card' }, [
        vue.h('span', null, `clarify:${JSON.stringify(props.clarify || [])}`),
        vue.h('span', null, `unsafe:${props.unsafe ? 1 : 0}`),
      ])
  },
}

function loadEsModule(code, filename, resolve) {
  const js = ts.transpileModule(code, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText
  const module = { exports: {} }
  const run = new vm.Script(`(function (exports, require, module) {\n${js}\n})`, { filename })
  run.runInContext(vm.createContext({ console }))(module.exports, resolve, module)
  return module.exports
}

let cachedChat = null
function loadUseChat() {
  if (cachedChat) return cachedChat
  const source = fs.readFileSync(USECHAT_PATH, 'utf8')
  cachedChat = loadEsModule(source, 'useChat.ts', (specifier) => {
    if (specifier === 'vue') return vue
    // useChat 的其余导入只为 useNl2sqlChat 提供运行时依赖；snapshotTurn
    // 是纯函数，不依赖它们，因此用空模块替换即可（不执行页面流程）。
    return {}
  })
  return cachedChat
}

let cachedMessageList = null
function compileMessageList() {
  if (cachedMessageList) return cachedMessageList
  const source = fs.readFileSync(MESSAGELIST_PATH, 'utf8')
  const { descriptor, errors } = parse(source, { filename: MESSAGELIST_PATH })
  assert.equal(errors.length, 0, 'MessageList.vue 解析失败')
  const script = compileScript(descriptor, { id: 'ml-test', inlineTemplate: true })
  cachedMessageList = loadEsModule(script.content, 'MessageList-script.mjs', (specifier) => {
    if (specifier === 'vue') return vue
    if (specifier.includes('AnswerCard')) return { default: AnswerCardStub }
    if (specifier.includes('format')) return { formatTurnDivider: () => '今天' }
    return {}
  }).default
  return cachedMessageList
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

function mountMessageList(props) {
  const container = { children: [], parentNode: null }
  const app = renderer.createApp({
    setup() {
      return () => vue.h(compileMessageList(), props)
    },
  })
  app.mount(container)
  return { container, app, text: textOf(container) }
}

function baseProps(overrides = {}) {
  return {
    turns: [],
    turnsLoading: false,
    turnsError: '',
    hasEarlier: false,
    currentTurn: null,
    submitting: false,
    elapsedSeconds: 0,
    sessionId: 's1',
    ...overrides,
  }
}

function makeResult(openQuestions, overrides = {}) {
  return {
    session_id: 's1',
    thread_id: 'dbda:s1:1',
    request_id: 'r1',
    run_id: 'run1',
    status: 'completed',
    answer: '回答',
    findings: [],
    evidence: [],
    metric_definitions: [],
    limitations: [],
    open_questions: openQuestions,
    errors: [],
    warnings: [],
    archived: true,
    stats: { elapsed_ms: 1 },
    source_id: 'orders',
    history_saved: true,
    error_code: null,
    ...overrides,
  }
}

// ------------------------------------------------------------- snapshotTurn 三态

test('snapshotTurn：明确无问题写 []，有补充问题原样复制（不共享数组）', () => {
  const { snapshotTurn } = loadUseChat()
  const emptyResult = makeResult([])
  const emptyTurn = snapshotTurn('问', emptyResult)
  // 数组来自 vm 上下文（跨 realm），用 Array.from 归一后再深比较
  assert.deepEqual(Array.from(emptyTurn.open_questions), [])

  const questions = ['权威问题', '补充问题']
  const turn = snapshotTurn('问', makeResult(questions))
  assert.deepEqual(Array.from(turn.open_questions), questions)
  assert.notStrictEqual(turn.open_questions, questions, '必须复制而非共享数组')
})

test('snapshotTurn：completed 状态下非空 open_questions 也保留（不限于 needs_clarification）', () => {
  const { snapshotTurn } = loadUseChat()
  const turn = snapshotTurn('问', makeResult(['非阻塞建议']))
  assert.equal(turn.open_questions.length, 1)
})

test('snapshotTurn：不安全终态安全投影写 [] 且不带回结果引用', () => {
  const { snapshotTurn } = loadUseChat()
  const unsafe = makeResult(['不应泄漏的问题'], {
    error_code: 'knowledge_changed',
    evidence: [{ sql: 'SELECT 1', result_ref: 'res_x', row_count: 1, source_truncated: false }],
  })
  const turn = snapshotTurn('问', unsafe)
  assert.deepEqual(Array.from(turn.open_questions), [], '原问题不得写入历史')
  assert.deepEqual(Array.from(turn.evidence), [], '不安全终态不带回 SQL/结果引用')
})

// ------------------------------------------------------------- 历史三态

test('历史轮次三态：null=未知不显示、[]=明确无问题不显示、非空显示', () => {
  const cases = [
    { open_questions: null, expected: 'clarify:[]' },
    { open_questions: [], expected: 'clarify:[]' },
    { open_questions: ['问题一'], expected: 'clarify:["问题一"]' },
  ]
  for (const item of cases) {
    const mounted = mountMessageList(
      baseProps({
        turns: [
          {
            question: '历史问',
            answer: '历史答',
            saved_at: '2026-11-13T10:00:00+08:00',
            open_questions: item.open_questions,
          },
        ],
      }),
    )
    assert.ok(mounted.text.includes(item.expected), `三态 ${JSON.stringify(item)}: ${mounted.text}`)
    mounted.app.unmount()
  }
})

// ------------------------------------------------------------- 实时 / 快照 / 历史一致

test('实时快照与刷新后历史一致：非空问题在实时与历史都显示', () => {
  const { snapshotTurn } = loadUseChat()
  const result = makeResult(['补充问题一', '补充问题二'])
  const turn = snapshotTurn('问', result)

  const live = mountMessageList(
    baseProps({
      currentTurn: { question: '问', result, error: null, errorMessage: '' },
    }),
  )
  assert.ok(live.text.includes('clarify:["补充问题一","补充问题二"]'), live.text)
  live.app.unmount()

  const history = mountMessageList(baseProps({ turns: [turn] }))
  assert.ok(
    history.text.includes('clarify:["补充问题一","补充问题二"]'),
    history.text,
  )
  history.app.unmount()
})

test('实时轮次不安全终态：原问题被抑制，且不泄漏到历史快照', () => {
  const { snapshotTurn } = loadUseChat()
  const result = makeResult(['敏感问题'], { error_code: 'dependency_unavailable' })
  const live = mountMessageList(
    baseProps({
      currentTurn: { question: '问', result, error: null, errorMessage: '' },
    }),
  )
  assert.ok(live.text.includes('clarify:[]'), live.text)
  assert.ok(live.text.includes('unsafe:1'), live.text)
  assert.ok(!live.text.includes('敏感问题'), '实时不得泄漏原问题')
  live.app.unmount()

  const turn = snapshotTurn('问', result)
  const history = mountMessageList(baseProps({ turns: [turn] }))
  assert.ok(history.text.includes('clarify:[]'), history.text)
  assert.ok(!history.text.includes('敏感问题'), '历史不得泄漏原问题')
  history.app.unmount()
})
