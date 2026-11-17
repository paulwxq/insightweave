<script setup lang="ts">
/**
 * 空状态（doc 6.3 §5.3）：图标盒 + 主文案 + 副文案，可选操作区。
 * 图标为 Lucide 风格线性 SVG，不用彩色 emoji 或字符图标。
 */
defineProps<{
  title: string
  description?: string
}>()
</script>

<template>
  <div class="empty-state-box" role="status">
    <span class="empty-state-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24">
        <path d="M22 12h-6l-2 3h-4l-2-3H2" />
        <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
      </svg>
    </span>
    <h3>{{ title }}</h3>
    <p v-if="description">{{ description }}</p>
    <div v-if="$slots.action" class="empty-state-action">
      <slot name="action" />
    </div>
  </div>
</template>

<style scoped>
.empty-state-box {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  text-align: center;
  padding: 34px 20px;
  color: #66788f;
}
.empty-state-icon {
  display: grid;
  place-items: center;
  width: 52px;
  height: 52px;
  border-radius: 16px;
  background: #e3eefb;
  color: #2e7cd6;
  margin-bottom: 6px;
}
.empty-state-icon svg {
  width: 22px;
  height: 22px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.empty-state-box h3 {
  margin: 0;
  font-size: 15px;
  color: #17293f;
}
.empty-state-box p {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  max-width: 420px;
}
.empty-state-action {
  margin-top: 10px;
}
</style>
