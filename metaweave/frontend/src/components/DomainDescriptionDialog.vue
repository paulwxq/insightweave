<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

const props = defineProps<{
  initialText: string
  initialSync: boolean
}>()

const emit = defineEmits<{
  cancel: []
  save: [text: string, sync: boolean]
}>()

// textarea 与复选框都是草稿：保存只写入页面已保存状态；取消/叉号只丢弃草稿。
const draft = ref(props.initialText)
const syncChecked = ref(props.initialSync)
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
  emit('save', text, syncChecked.value)
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
      <button type="button" class="dialog-close" aria-label="关闭" @click="close">✕</button>
      <p id="domain-description-title">Domain 生成描述</p>
      <p class="dialog-note">
        优先取本作业上次使用的描述，没有时取数据源登记的描述；这里的修改只用于当前作业。勾选下方选项后，点击执行时会把该描述同时保存到数据源信息。
      </p>
      <label class="field">
        <span>描述内容</span>
        <textarea v-model="draft" rows="5"></textarea>
        <span class="character-count">{{ charCount }}/1000</span>
      </label>
      <label class="dialog-check">
        <input v-model="syncChecked" type="checkbox" />
        <span>执行时同步到数据源描述</span>
      </label>
      <p v-if="error" class="status-message" role="alert">{{ error }}</p>
      <div class="dialog-actions">
        <button type="button" class="button button-primary" @click="submit">保存</button>
      </div>
    </div>
  </div>
</template>
