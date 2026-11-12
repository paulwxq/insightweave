/**
 * QuestionInput 组件测试（doc11 / Codex R1-03）：实际 SFC 经 compiler-sfc 编译、
 * 真实 Vue createRenderer 挂载，用真实事件驱动——覆盖 IME 组合输入的回车保护。
 * 不新增依赖：@vue/compiler-sfc、vue、node:test 均为现有环境。
 */
const assert = require('node:assert/strict')
const { test } = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')
const { parse, compileScript } = require('@vue/compiler-sfc')

const COMPONENT_PATH = path.join(__dirname, 'QuestionInput.vue')

// ---------------------------------------------------------------- 假元素宿主（带事件）

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

function makeEvent(type, props = {}) {
  const event = new Event(type, { cancelable: true })
  Object.assign(event, props)
  return event
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

// ---------------------------------------------------------------- 编译真实 SFC

function loadEsModule(code, filename, resolve) {
  const js = ts.transpileModule(code, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText
  const module = { exports: {} }
  const run = new vm.Script(`(function (exports, require, module) {\n${js}\n})`, { filename })
  const requireStub = (specifier) => resolve(specifier)
  run.runInContext(vm.createContext({ console }))(module.exports, requireStub, module)
  return module.exports
}

function compileQuestionInput() {
  const source = fs.readFileSync(COMPONENT_PATH, 'utf8')
  const { descriptor, errors } = parse(source, { filename: COMPONENT_PATH })
  assert.equal(errors.length, 0, 'SFC 解析失败')
  // inlineTemplate：模板编译进 setup，绑定以闭包访问——执行真实 SFC 逻辑
  const script = compileScript(descriptor, { id: 'qi-test', inlineTemplate: true })
  const scriptModule = loadEsModule(script.content, 'QuestionInput-script.mjs', (specifier) => {
    if (specifier === 'vue') return vue
    if (specifier === '../format') {
      return loadEsModule(
        fs.readFileSync(path.join(__dirname, '../format.ts'), 'utf8'),
        'format.mjs',
        () => ({}),
      )
    }
    throw new Error(`未替换依赖：${specifier}`)
  })
  return scriptModule.default
}

// ---------------------------------------------------------------- 挂载与事件工具

function mountComponent(Component, props) {
  const emitted = []
  const container = { children: [], parentNode: null }
  const app = renderer.createApp(Component, props)
  const originalSub = app.mount.bind(app)
  const root = originalSub(container)
  return { app, container, emitted }
}

function findTextarea(root) {
  const walk = (node) => {
    if (!node) return null
    if (node instanceof FakeElement && node.tag === 'textarea') return node
    for (const child of node.children || []) {
      const found = walk(child)
      if (found) return found
    }
    return null
  }
  return walk(root)
}

function findSendButton(root) {
  const buttons = []
  const walk = (node) => {
    if (!node) return
    if (node instanceof FakeElement && node.tag === 'button') buttons.push(node)
    for (const child of node.children || []) walk(child)
  }
  walk(root)
  return buttons.find((b) => b.attributes['aria-label'] === '发送')
}

function freshProps(overrides = {}) {
  return {
    modelValue: '',
    disabled: false,
    blockedReason: '',
    submitting: false,
    creating: false,
    elapsedSeconds: 0,
    sourceLabel: 'A库',
    ...overrides,
  }
}

// ---------------------------------------------------------------- 用例

test('普通输入与回车：非组合状态正常写草稿并发送', async () => {
  const Component = compileQuestionInput()
  const emitted = []
  const state = vue.reactive({ modelValue: '' })
  const container = { children: [], parentNode: null }
  const app = renderer.createApp({
    setup() {
      return () =>
        vue.h(Component, {
          ...freshProps(),
          modelValue: state.modelValue,
          'onUpdate:modelValue': (v) => {
            state.modelValue = v
            emitted.push(['update', v])
          },
          onSubmit: (q) => emitted.push(['submit', q]),
        })
    },
  })
  app.mount(container)
  const textarea = findTextarea(container)
  assert.ok(textarea, 'textarea 已渲染')

  textarea.value = '统计客户数'
  textarea.dispatchEvent(makeEvent('input'))
  await new Promise((r) => setImmediate(r))
  assert.equal(state.modelValue, '统计客户数', '普通输入写入受控草稿')

  const enter = makeEvent('keydown', { key: 'Enter', isComposing: false, keyCode: 13 })
  textarea.dispatchEvent(enter)
  await new Promise((r) => setImmediate(r))
  const submits = emitted.filter((e) => e[0] === 'submit')
  assert.equal(submits.length, 1)
  assert.equal(submits[0][1], '统计客户数')
  assert.equal(enter.defaultPrevented, true, '普通回车阻止默认换行')
  app.unmount()
})

test('IME 组合输入：组合中回车不发送、不拦截；组合结束后草稿为最终文字', async () => {
  const Component = compileQuestionInput()
  const emitted = []
  const state = vue.reactive({ modelValue: '' })
  const container = { children: [], parentNode: null }
  const app = renderer.createApp({
    setup() {
      return () =>
        vue.h(Component, {
          ...freshProps(),
          modelValue: state.modelValue,
          'onUpdate:modelValue': (v) => {
            state.modelValue = v
            emitted.push(['update', v])
          },
          onSubmit: (q) => emitted.push(['submit', q]),
        })
    },
  })
  app.mount(container)
  const textarea = findTextarea(container)

  // 组合开始：输入中间态“北”
  textarea.dispatchEvent(makeEvent('compositionstart'))
  textarea.value = '北'
  textarea.dispatchEvent(makeEvent('input'))
  await new Promise((r) => setImmediate(r))
  assert.equal(state.modelValue, '', '组合中间态不写入受控草稿（原生 v-model 组合保护）')

  // 组合中的选字回车（isComposing=true / keyCode 229）：不发送、不 preventDefault
  const imeEnter = makeEvent('keydown', { key: 'Enter', isComposing: true, keyCode: 229 })
  textarea.dispatchEvent(imeEnter)
  await new Promise((r) => setImmediate(r))
  assert.equal(emitted.filter((e) => e[0] === 'submit').length, 0, '组合中回车不得发送')
  assert.equal(imeEnter.defaultPrevented, false, '组合中回车不得拦截选字')

  // 组合结束：v-model 同步最终文字
  textarea.value = '北京客户数'
  textarea.dispatchEvent(makeEvent('compositionend'))
  await new Promise((r) => setImmediate(r))
  assert.equal(state.modelValue, '北京客户数', '组合结束同步最终文字')

  // 组合结束后的普通回车正常发送
  const plainEnter = makeEvent('keydown', { key: 'Enter', isComposing: false, keyCode: 13 })
  textarea.dispatchEvent(plainEnter)
  await new Promise((r) => setImmediate(r))
  const submits = emitted.filter((e) => e[0] === 'submit')
  assert.equal(submits.length, 1)
  assert.equal(submits[0][1], '北京客户数', '发送完整最终文字')
  assert.equal(plainEnter.defaultPrevented, true)
  app.unmount()
})

test('Shift+Enter 不发送且不拦截；按钮发送正常；禁用状态不发送', async () => {
  const Component = compileQuestionInput()
  const emitted = []
  const state = vue.reactive({ modelValue: '按住Shift的问题' })
  const container = { children: [], parentNode: null }
  const makeApp = (extra = {}) =>
    renderer.createApp({
      setup() {
        return () =>
          vue.h(Component, {
            ...freshProps(),
            ...extra,
            modelValue: state.modelValue,
            'onUpdate:modelValue': (v) => {
              state.modelValue = v
            },
            onSubmit: (q) => emitted.push(['submit', q]),
          })
      },
    })
  let app = makeApp()
  app.mount(container)
  const textarea = findTextarea(container)
  const shiftEnter = makeEvent('keydown', { key: 'Enter', shiftKey: true, isComposing: false, keyCode: 13 })
  textarea.dispatchEvent(shiftEnter)
  await new Promise((r) => setImmediate(r))
  assert.equal(emitted.length, 0, 'Shift+Enter 不发送')
  assert.equal(shiftEnter.defaultPrevented, false, 'Shift+Enter 不拦截换行')

  const button = findSendButton(container)
  assert.ok(button, '发送按钮已渲染')
  assert.equal(button.disabled, false)
  button.dispatchEvent(makeEvent('click'))
  await new Promise((r) => setImmediate(r))
  assert.equal(emitted.length, 1, '按钮发送正常')
  assert.equal(emitted[0][1], '按住Shift的问题')
  app.unmount()

  // 禁用状态：回车与按钮都不发送
  emitted.length = 0
  const container2 = { children: [], parentNode: null }
  app = makeApp({ disabled: true, submitting: false, creating: false })
  app.mount(container2)
  const textarea2 = findTextarea(container2)
  const enter2 = makeEvent('keydown', { key: 'Enter', isComposing: false, keyCode: 13 })
  textarea2.dispatchEvent(enter2)
  const button2 = findSendButton(container2)
  button2.dispatchEvent(makeEvent('click'))
  await new Promise((r) => setImmediate(r))
  assert.equal(emitted.length, 0, '禁用状态不发送')
  assert.equal(enter2.defaultPrevented, false)
  app.unmount()
})
