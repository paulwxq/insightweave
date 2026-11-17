<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

const props = withDefaults(
  defineProps<{
    initialText: string
    /** 场景说明；默认是“写入版本”的口径，临时输入等场景由调用方覆盖 */
    note?: string
  }>(),
  {
    note: '保存作业后写入该版本。工作区还没有 db_domains.yaml 时，生成会用这段文字；文件已存在则不覆盖其中的描述。',
  },
)

const emit = defineEmits<{
  cancel: []
  save: [text: string]
}>()

// textarea 是草稿：保存只写入调用方的已保存状态；取消/叉号只丢弃草稿。
const draft = ref(props.initialText)
const error = ref('')

// 长度按 Unicode 码点计数（与后端 Python len 一致）；不用原生 maxlength——
// 它按 UTF-16 码元计数，emoji 等字符会与后端口径不同。
const charCount = computed(() => [...draft.value].length)

function submit() {
  const text = draft.value.trim()
  if (!text) {
    error.value = '描述不能为空，也不能只有空白字符'
    return
  }
  if ([...text].length > 1000) {
    error.value = '描述不能超过 1000 个字符'
    return
  }
  emit('save', text)
}

function close() {
  emit('cancel')
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') close()
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div class="dialog-backdrop" @click.self="close">
    <div
      class="dialog-panel with-close"
      role="dialog"
      aria-modal="true"
      aria-labelledby="domain-description-title"
    >
      <button type="button" class="dialog-close" aria-label="关闭" @click="close"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></button>
      <p id="domain-description-title">Domain 描述</p>
      <p class="dialog-note">{{ note }}</p>
      <label class="field">
        <span>描述内容</span>
        <textarea v-model="draft" rows="5"></textarea>
        <span class="character-count">{{ charCount }}/1000</span>
      </label>
      <p v-if="error" class="status-message" role="alert">{{ error }}</p>
      <div class="dialog-actions">
        <button type="button" class="button button-primary" @click="submit">保存</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.field textarea {
  font-size: 12px;
  line-height: 1.5;
  padding: 8px 10px;
}
</style>
