<script setup lang="ts">
defineProps<{ title: string; description: string }>()
</script>

<template>
  <section class="placeholder-panel">
    <div class="placeholder-symbol">◇</div>
    <div>
      <h3>{{ title }}</h3>
      <p>{{ description }}</p>
    </div>
    <span class="pill pill-muted">待接入</span>
  </section>
</template>
