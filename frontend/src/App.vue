<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import AppSidebar from './components/AppSidebar.vue'

const route = useRoute()
const queryPage = computed(() => route.path === '/query' || route.path.startsWith('/query/'))
</script>

<template>
  <div class="app-shell">
    <AppSidebar />
    <main class="page-content" :class="{ 'page-content-query': queryPage }">
      <RouterView />
    </main>
  </div>
</template>
