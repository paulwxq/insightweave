<script setup lang="ts">
// 404 / 未知路由（doc 6.3 §8.1 全局错误页）：复用统一壳子与空状态样式
import EmptyState from '../components/EmptyState.vue'
</script>

<template>
  <div class="nf-page">
    <span class="ds-kicker">NOT FOUND</span>
    <h1>页面不存在</h1>
    <p>地址可能已变更或输入有误；请从下方入口返回。</p>
    <EmptyState
      title="未找到该页面"
      description="检查地址是否正确，或返回首页 / 工作区管理继续操作。"
    >
      <template #action>
        <div class="nf-actions">
          <RouterLink to="/" class="button button-outline">返回首页</RouterLink>
          <RouterLink to="/workspaces" class="button button-primary">工作区管理</RouterLink>
        </div>
      </template>
    </EmptyState>
  </div>
</template>

<style scoped>
.nf-page {
  max-width: 640px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.nf-page h1 {
  margin: 8px 0 0;
  font-family: "Noto Serif SC", "Songti SC", Georgia, serif;
  font-size: 30px;
  color: #17293f;
}
.nf-page > p {
  margin: 0;
  color: #66788f;
  font-size: 13px;
}
.nf-actions {
  display: flex;
  gap: 10px;
  justify-content: center;
}
</style>
