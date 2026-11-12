<script setup lang="ts">
import { computed, provide, ref } from 'vue'
import { useRoute } from 'vue-router'
import AppSidebar from './components/AppSidebar.vue'
import { queryBusyKey } from './modules/nl2sql/queryBusy'

const route = useRoute()
const queryWorkspace = computed(
  () => route.path === '/query/new' || /^\/query\/[^/]+$/.test(route.path),
)
provide(queryBusyKey, ref(false))
</script>

<template>
  <div class="app-shell">
    <AppSidebar />
    <main class="page-content" :class="{ 'page-content-query': queryWorkspace }">
      <RouterView />
    </main>
  </div>
</template>
