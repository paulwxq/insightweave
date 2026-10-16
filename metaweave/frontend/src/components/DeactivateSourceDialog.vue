<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'

const props = defineProps<{
  displayName: string
  busy: boolean
  error: string
}>()

const emit = defineEmits<{
  cancel: []
  confirm: []
}>()

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
    <div class="dialog-panel" role="dialog" aria-modal="true" aria-labelledby="deactivate-source-title">
      <p id="deactivate-source-title">禁用数据源“{{ displayName }}”？</p>
      <p class="dialog-note">
        禁用后，该数据源将不能创建新的采集作业和画像。已登记的连接信息、历史作业记录与已入库的画像不受影响。
      </p>
      <p v-if="error" class="status-message" role="alert">{{ error }}</p>
      <div class="dialog-actions">
        <button type="button" class="button button-outline" :disabled="busy" @click="close">取消</button>
        <button type="button" class="button button-primary" :disabled="busy" @click="emit('confirm')">确认禁用</button>
      </div>
    </div>
  </div>
</template>
