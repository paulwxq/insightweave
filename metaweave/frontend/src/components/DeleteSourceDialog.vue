<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'

const props = defineProps<{
  displayName: string
  busy: boolean
  error: string
}>()

const emit = defineEmits<{
  cancel: []
  confirm: [deleteOutput: boolean]
}>()

const deleteOutput = ref(false)

function close() {
  if (props.busy) return
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
    <div class="dialog-panel" role="dialog" aria-modal="true" aria-labelledby="delete-source-title">
      <p id="delete-source-title">确认删除数据源“{{ displayName }}”？</p>
      <p class="dialog-note">删除后不可恢复。请先确认该数据源没有正在执行的采集；采集过程中删除，可能留下不完整文件或新的孤立目录。</p>
      <label class="dialog-check">
        <input v-model="deleteOutput" type="checkbox" :disabled="busy" />
        <span>同时删除此数据源在 output 下的目录及其全部内容</span>
      </label>
      <p v-if="error" class="status-message" role="alert">{{ error }}</p>
      <div class="dialog-actions">
        <button type="button" class="button button-outline" :disabled="busy" @click="close">取消</button>
        <button type="button" class="button button-primary" :disabled="busy" @click="emit('confirm', deleteOutput)">确认删除</button>
      </div>
    </div>
  </div>
</template>
