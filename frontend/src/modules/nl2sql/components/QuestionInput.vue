<script setup lang="ts">
/**
 * 底部提问框（doc 11：受控草稿）。草稿由 useChat 持有，创建失败或地址切换不丢输入；
 * 发送事件不清空文本，由状态层在实际提交时清理。
 * 经 v-model（可写 computed）接入，保留 Vue 对 IME 组合输入的原生保护：
 * 组合中间态不写入受控草稿；组合中的回车用于选字，不发送、不拦截。
 * 数据库名称只作显示：选择和就绪检查在对话顶部，且只在新建时进行。
 */
import { computed } from 'vue'
import { codepointLength, QUESTION_MAX } from '../format'

const props = defineProps<{
  modelValue: string
  disabled: boolean
  blockedReason: string
  submitting: boolean
  creating: boolean
  elapsedSeconds: number
  sourceLabel: string
}>()

const emit = defineEmits<{
  submit: [question: string]
  'update:modelValue': [value: string]
}>()

/** v-model 绑定到受控草稿：组合结束后才同步，获得与原生 v-model 一致的 IME 行为。 */
const draft = computed<string>({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})

const length = computed(() => codepointLength(props.modelValue))
const tooLong = computed(() => length.value > QUESTION_MAX)
const blank = computed(() => props.modelValue.trim() === '')
const canSubmit = computed(
  () => !props.disabled && !props.submitting && !props.creating && !blank.value && !tooLong.value,
)

function onEnterKeydown(event: KeyboardEvent) {
  // IME 组合中的回车是选字确认：不发送，也不 preventDefault 干扰输入法
  if (event.isComposing || event.keyCode === 229) return
  if (event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return
  if (!canSubmit.value) return
  event.preventDefault()
  emit('submit', props.modelValue)
}

function submit() {
  if (!canSubmit.value) return
  emit('submit', props.modelValue)
}
</script>

<template>
  <div class="composer-wrap">
    <p v-if="blockedReason" class="blocked">
      <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6" /><path d="M8 7.2V11" /><path d="M8 5h.01" /></svg>
      <span>{{ blockedReason }}</span>
    </p>
    <div class="composer">
      <textarea
        v-model="draft"
        :disabled="disabled"
        rows="2"
        placeholder="用自然语言描述你的查询，例如：统计上个月各品类的销售额与环比变化…"
        @keydown.enter="onEnterKeydown"
      ></textarea>
      <div class="composer-bar">
        <span class="source-chip" :class="{ chosen: sourceLabel }" :title="sourceLabel || '尚未选择数据库'">
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <ellipse cx="8" cy="4" rx="5" ry="2" />
            <path d="M3 4v8c0 1.1 2.2 2 5 2s5-.9 5-2V4" />
            <path d="M3 8c0 1.1 2.2 2 5 2s5-.9 5-2" />
          </svg>
          {{ sourceLabel || '未选择数据库' }}
        </span>
        <button type="button" class="send" :disabled="!canSubmit" aria-label="发送" @click="submit">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.4 3.1 7.6h3.2V13.6h3.4V7.6h3.2z" /></svg>
        </button>
      </div>
    </div>
    <p v-if="tooLong" class="warn">超出 {{ QUESTION_MAX }} 个字符上限（当前 {{ length }}）</p>
    <p v-else-if="submitting" class="meta">正在执行，已等待 {{ elapsedSeconds }} 秒</p>
    <p v-else-if="creating" class="meta">正在创建会话…</p>
  </div>
</template>

<style scoped>
.composer-wrap {
  background: #fff;
  border-top: 1px solid #d9e3ef;
  padding: 16px 24px 24px;
  display: grid;
  gap: 12px;
}

.blocked {
  margin: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 10px;
  background: #f7fafd;
  color: #66788f;
  font-size: 12px;
}

.blocked svg {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.warn,
.meta {
  margin: 0;
  font-size: 12px;
  color: #9a6b32;
}

.warn {
  color: #a54756;
}

.meta {
  color: #66788f;
}

.composer {
  background: #f7f9fc;
  border: 1px solid #d9e3ef;
  border-radius: 16px;
  padding: 16px 16px 14px;
  display: grid;
  gap: 18px;
}

.composer textarea {
  width: 100%;
  border: 0;
  background: transparent;
  resize: none;
  min-height: 44px;
  outline: none;
  color: #17293f;
  font-size: 13px;
  line-height: 21px;
}

.composer textarea::placeholder {
  color: #8a99ac;
}

.composer textarea:disabled {
  color: #8aa0b8;
}

.composer-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.source-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: min(280px, 60%);
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 8px;
  padding: 7px 10px;
  color: #66788f;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-chip.chosen {
  color: #17293f;
}

.source-chip svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  flex-shrink: 0;
}

.send {
  width: 38px;
  height: 38px;
  border: 0;
  border-radius: 10px;
  background: #2e7cd6;
  color: #fff;
  display: grid;
  place-items: center;
  flex-shrink: 0;
}

.send svg {
  width: 17px;
  height: 17px;
  fill: currentColor;
}

.send:disabled {
  background: #dce8f5;
  color: #8faac7;
}
</style>
