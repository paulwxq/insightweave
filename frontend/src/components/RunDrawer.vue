<script setup lang="ts">
// 运行详情抽屉：运行整体状态 + 逐步骤结果；点击步骤行可打开步骤详情抽屉
import RunPanel from './RunPanel.vue'
import type { JobKind, RunDetail } from '../api/types'

defineProps<{
  kind: JobKind
  run: RunDetail | null
  loading: boolean
  error: string
}>()

const emit = defineEmits<{ close: []; retry: []; 'open-step': [step: string] }>()

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
}
</script>

<template>
  <div class="dialog-backdrop" @click.self="emit('close')" @keydown="onKeydown">
    <div
      class="dialog-panel with-close run-drawer"
      role="dialog"
      aria-modal="true"
      aria-label="运行详情"
      tabindex="0"
    >
      <button type="button" class="dialog-close" aria-label="关闭" @click="emit('close')"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></button>
      <p class="run-drawer-title">运行详情</p>
      <RunPanel
        :kind="kind"
        :run="run"
        :loading="loading"
        :error="error"
        @retry="emit('retry')"
        @open-step="(step: string) => emit('open-step', step)"
      />
    </div>
  </div>
</template>

<style scoped>
.run-drawer {
  width: min(100%, 720px);
  max-height: min(86vh, 780px);
  overflow: auto;
}
.run-drawer-title {
  font-weight: 700;
}
</style>
