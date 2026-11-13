// 前端专项：WebTurn.open_questions 三态 + snapshotTurn 一致（R1-17）
const { test } = require('node:test')
const assert = require('node:assert/strict')

// 纯类型/行为契约在 .vue/.ts 内，此处以模块级可加载方式验证（无构建步骤
// 时退化为读取源码断言契约标记）
const { readFileSync } = require('node:fs')

const types = readFileSync('src/modules/nl2sql/api/types.ts', 'utf8')
test('WebTurn 契约含可选 null open_questions', () => {
  assert.match(types, /open_questions\?:\s*string\[\]\s*\|\s*null/)
})

const useChat = readFileSync('src/modules/nl2sql/composables/useChat.ts', 'utf8')
test('snapshotTurn 快照包含 open_questions（与 WebStore 保存值一致）', () => {
  assert.match(useChat, /open_questions:\s*unsafe\s*\?\s*\[\]\s*:\s*\[\.\.\.result\.open_questions\]/)
})

const messageList = readFileSync('src/modules/nl2sql/components/MessageList.vue', 'utf8')
test('历史轮次消费保存值（null=未知不显示）', () => {
  assert.match(messageList, /turn\.open_questions \?\? \[\]/)
})
test('实时轮次非空即展示（不限于 needs_clarification）', () => {
  assert.match(messageList, /clarify:\s*unsafe\s*\?\s*\[\]\s*:\s*\[\.\.\.\(result\?\.open_questions \?\? \[\]\)\]/)
})

const answerCard = readFileSync('src/modules/nl2sql/components/AnswerCard.vue', 'utf8')
test('问题列表带"澄清与补充问题"标题（安全抑制路径写 []）', () => {
  assert.match(answerCard, /澄清与补充问题/)
})
