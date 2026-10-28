<script setup lang="ts">
/**
 * 工作区二级导航（doc 6.3 §3.3）：概览 / 采集作业 / 发布作业。
 * 采集与发布作业不是全局菜单——必须带 workspace_id 才能确定数据范围。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'

export type WorkspaceNavKey = 'overview' | 'collection' | 'publish'

const props = defineProps<{
  workspaceId: string
  current: WorkspaceNavKey
}>()

const route = useRoute()

const items = computed(() => [
  {
    key: 'overview' as const,
    label: '概览',
    to: `/workspaces/${props.workspaceId}`,
  },
  {
    key: 'collection' as const,
    label: '采集作业',
    to: `/workspaces/${props.workspaceId}/collection`,
  },
  {
    key: 'publish' as const,
    label: '发布作业',
    to: `/workspaces/${props.workspaceId}/publish`,
  },
])

function isActive(item: { key: WorkspaceNavKey; to: string }): boolean {
  if (item.key !== props.current) return false
  // 概览仅在确切路径激活；作业页在列表/新建/详情/执行整段激活
  return item.key === 'overview' ? route.path === item.to : route.path.startsWith(item.to)
}
</script>

<template>
  <nav class="ws-nav" aria-label="工作区导航">
    <RouterLink
      v-for="item in items"
      :key="item.key"
      :to="item.to"
      class="ws-nav-item"
      :class="{ current: isActive(item) }"
      :aria-current="isActive(item) ? 'page' : undefined"
    >
      {{ item.label }}
    </RouterLink>
  </nav>
</template>

<style scoped>
.ws-nav {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.ws-nav-item {
  border: 1px solid #d9e3ef;
  border-radius: 10px;
  background: #fff;
  color: #66788f;
  font-size: 13px;
  font-weight: 600;
  padding: 8px 16px;
}
.ws-nav-item:hover {
  color: #2e7cd6;
}
.ws-nav-item.current {
  background: #eef3f9;
  border-color: #d9e3ef;
  color: #2e7cd6;
}
</style>
