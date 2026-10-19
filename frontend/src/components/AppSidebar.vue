<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ConversationList from '../modules/nl2sql/components/ConversationList.vue'
import {
  activeSessionId,
  loadMoreSessions,
  refreshSessions,
  sessions,
  sessionsError,
  sessionsHasMore,
  sessionsLoading,
  sessionsNotice,
} from '../modules/nl2sql/sessionList'

const route = useRoute()
const router = useRouter()
const assetsActive = computed(
  () => route.path === '/data-assets' || route.path.startsWith('/data-assets/'),
)
const sourcesActive = computed(
  () => route.path === '/data-sources' || route.path.startsWith('/data-sources/'),
)
const collectionActive = computed(
  () => route.path === '/data-collection' || route.path.startsWith('/data-collection/'),
)
const publishActive = computed(
  () => route.path === '/profile-publishing' || route.path.startsWith('/profile-publishing/'),
)
const queryActive = computed(
  () => route.path === '/query' || route.path.startsWith('/query/'),
)

function openSession(sessionId: string) {
  void router.push({ name: 'nl2sql-query-session', params: { sessionId } })
}
</script>

<template>
  <aside class="sidebar" :class="{ 'sidebar-lock': queryActive }">
    <div class="brand">
      <img class="brand-logo" src="/favicon.svg" width="34" height="34" alt="" />
      <div class="brand-text">
        <strong>MetaWeave</strong>
        <small>智能数据分析工作台</small>
      </div>
    </div>

    <div class="sidebar-actions">
      <RouterLink to="/query" class="button button-primary full-width query-entry">›&nbsp; 新建查询</RouterLink>
      <button class="button button-outline full-width" disabled title="后续阶段开放">＋&nbsp; 新建分析</button>
    </div>

    <nav class="sidebar-nav" aria-label="主导航">
      <RouterLink to="/" exact-active-class="active"><span class="nav-icon">⌂</span>首页</RouterLink>
      <RouterLink to="/data-assets" custom v-slot="{ href, navigate }">
        <a :href="href" :class="{ active: assetsActive }" @click="navigate">
          <span class="nav-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5" /><path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3" /></svg>
          </span>
          数据资产
          <svg class="nav-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
        </a>
      </RouterLink>
      <RouterLink to="/data-sources" custom v-slot="{ href, navigate }">
        <a :href="href" class="nav-sub" :class="{ active: sourcesActive }" @click="navigate">
          <span class="nav-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><rect width="20" height="8" x="2" y="2" rx="2" /><rect width="20" height="8" x="2" y="14" rx="2" /><path d="M6 6h.01" /><path d="M6 18h.01" /></svg>
          </span>
          数据源管理
        </a>
      </RouterLink>
      <RouterLink to="/data-collection" custom v-slot="{ href, navigate }">
        <a :href="href" class="nav-sub" :class="{ active: collectionActive }" @click="navigate">
          <span class="nav-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M22 12h-4l-3 9L9 3l-3 9H2" /></svg>
          </span>
          数据采集
        </a>
      </RouterLink>
      <RouterLink to="/profile-publishing" custom v-slot="{ href, navigate }">
        <a :href="href" class="nav-sub" :class="{ active: publishActive }" @click="navigate">
          <span class="nav-icon" aria-hidden="true">
            <svg v-if="publishActive" viewBox="0 0 24 24">
              <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z" />
              <path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" />
              <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0" />
              <path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" />
            </svg>
            <svg v-else viewBox="0 0 24 24">
              <path d="M3 7V5a2 2 0 0 1 2-2h2" />
              <path d="M17 3h2a2 2 0 0 1 2 2v2" />
              <path d="M21 17v2a2 2 0 0 1-2 2h-2" />
              <path d="M7 21H5a2 2 0 0 1-2-2v-2" />
              <circle cx="12" cy="12" r="3" />
              <path d="m16 16-1.9-1.9" />
            </svg>
          </span>
          数据画像
        </a>
      </RouterLink>
      <span class="nav-disabled">
        <span class="nav-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" /><circle cx="12" cy="12" r="3" /></svg>
        </span>
        系统设置 <small>待开放</small>
      </span>
    </nav>

    <div v-if="queryActive" class="sidebar-history">
      <ConversationList
        :sessions="sessions"
        :loading="sessionsLoading"
        :error="sessionsError"
        :has-more="sessionsHasMore"
        :notice="sessionsNotice"
        :current-session-id="activeSessionId"
        @open="openSession"
        @load-more="loadMoreSessions"
        @refresh="refreshSessions"
      />
    </div>
    <div v-else class="sidebar-footer">
      <span class="eyebrow">RECENT PROJECTS</span>
      <p>分析项目待接入</p>
      <small>当前可登记并管理数据源连接。</small>
    </div>
  </aside>
</template>

<style scoped>
.sidebar-actions :deep(.button) {
  border-radius: 12px;
}

.sidebar-actions :deep(.query-entry) {
  background: #2e7cd6;
  box-shadow: none;
}

.sidebar-lock {
  height: 100vh;
  position: sticky;
  top: 0;
  overflow: hidden;
}

.sidebar-history {
  flex: 1;
  min-height: 0;
  display: flex;
  margin-top: 8px;
  padding-top: 14px;
  border-top: 1px solid #d9e3ef;
}
</style>
