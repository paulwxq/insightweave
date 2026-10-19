<script setup lang="ts">
/**
 * 底部提问框。数据库名称只作展示：选择和就绪检查在对话顶部，且只在新建时进行。
 */
import { computed, ref, watch } from 'vue'
import { codepointLength } from '../format'

const props = defineProps<{
  disabled: boolean
  blockedReason: string
  submitting: boolean
  elapsedSeconds: number
  draftEpoch: number
  sourceLabel: string
}>()

const emit = defineEmits<{ submit: [question: string] }>()

const QUESTION_MAX = 1000
const draft = ref('')

watch(
  () => props.draftEpoch,
  () => {
    draft.value = ''
  },
)

const length = computed(() => codepointLength(draft.value))
const tooLong = computed(() => length.value > QUESTION_MAX)
const blank = computed(() => draft.value.trim() === '')
const canSubmit = computed(
  () => !props.disabled && !props.submitting && !blank.value && !tooLong.value,
)

function submit() {
  if (!canSubmit.value) return
  emit('submit', draft.value)
  draft.value = ''
}
</script>

<template>
  <div class="composer-wrap">
    <p v-if="blockedReason" class="blocked">{{ blockedReason }}</p>
    <div class="composer">
      <textarea
        v-model="draft"
        :disabled="disabled"
        rows="2"
        placeholder="用自然语言描述你的查询，例如：统计上个月各品类的销售额与环比变化…"
        @keydown.enter.exact.prevent="submit"
      ></textarea>
      <div class="composer-bar">
        <span class="source-chip" :title="sourceLabel || '尚未选择数据库'">
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <ellipse cx="8" cy="4" rx="5" ry="2" />
            <path d="M3 4v8c0 1.1 2.2 2 5 2s5-.9 5-2V4" />
            <path d="M3 8c0 1.1 2.2 2 5 2s5-.9 5-2" />
          </svg>
          {{ sourceLabel || '未选择数据库' }}
        </span>
        <button type="button" class="send" :disabled="!canSubmit" aria-label="发送" @click="submit">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 12V4M5 7l3-3 3 3" /></svg>
        </button>
      </div>
    </div>
    <p v-if="tooLong" class="warn">超出 1000 个字符上限（当前 {{ length }}）</p>
    <p v-else-if="submitting" class="meta">正在执行，已等待 {{ elapsedSeconds }} 秒</p>
  </div>
</template>

<style scoped>
.composer-wrap {
  background: #fff;
  border-top: 1px solid #d9e3ef;
  padding: 14px 22px 16px;
  display: grid;
  gap: 8px;
}

.blocked,
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
  background: #f4f7fb;
  border: 1px solid #d9e3ef;
  border-radius: 16px;
  padding: 12px 14px 10px;
  display: grid;
  gap: 8px;
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
  border-radius: 999px;
  padding: 6px 12px;
  color: #41597a;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-chip svg,
.send svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  flex-shrink: 0;
}

.send {
  width: 36px;
  height: 36px;
  border: 0;
  border-radius: 12px;
  background: #2e7cd6;
  color: #fff;
  display: grid;
  place-items: center;
}

.send:disabled {
  background: #b7cbe4;
}
</style>
