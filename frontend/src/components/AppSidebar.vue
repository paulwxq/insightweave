<script setup lang="ts">
import { computed, ref } from 'vue'
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
// 工作区管理是独立全局导航项（doc 6.3 §3.3）；工作区内部页高亮它，
// 全局作业子菜单（设计稿 35/37）也归入该导航组
const workspacesActive = computed(
  () =>
    route.path === '/workspaces' ||
    route.path.startsWith('/workspaces/') ||
    route.path === '/collection-jobs' ||
    route.path === '/publish-jobs',
)
const collectionJobsActive = computed(() => route.path === '/collection-jobs')
const publishJobsActive = computed(() => route.path === '/publish-jobs')

// 工作区管理子菜单可折叠：进入全局作业页（含刷新/深链）默认展开，
// 其余入口默认收起；箭头向下 = 收起，向上 = 展开
const workspacesExpanded = ref(
  collectionJobsActive.value || publishJobsActive.value,
)
function toggleWorkspacesMenu() {
  workspacesExpanded.value = !workspacesExpanded.value
}
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
      <RouterLink to="/" exact-active-class="active">
        <span class="nav-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /><path d="M9 22V12h6v10" /></svg>
        </span>
        首页
      </RouterLink>
      <RouterLink to="/data-assets" custom v-slot="{ href, navigate }">
        <a :href="href" :class="{ active: assetsActive }" @click="navigate">
          <span class="nav-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5" /><path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3" /></svg>
          </span>
          数据资产
        </a>
      </RouterLink>
      <RouterLink to="/data-sources" custom v-slot="{ href, navigate }">
        <a :href="href" :class="{ active: sourcesActive }" @click="navigate">
          <span class="nav-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><rect width="20" height="8" x="2" y="2" rx="2" /><rect width="20" height="8" x="2" y="14" rx="2" /><path d="M6 6h.01" /><path d="M6 18h.01" /></svg>
          </span>
          数据源管理
        </a>
      </RouterLink>
      <RouterLink to="/workspaces" custom v-slot="{ href, navigate }">
        <a :href="href" :class="{ active: workspacesActive }" @click="(event) => { navigate(event); workspacesExpanded = true }">
          <span class="nav-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><rect width="18" height="18" x="3" y="3" rx="2" /><path d="M3 9h18" /><path d="M9 21V9" /></svg>
          </span>
          工作区管理
          <span
            class="nav-chevron"
            :class="{ open: workspacesExpanded }"
            role="button"
            tabindex="0"
            :aria-expanded="workspacesExpanded"
            aria-label="展开或收起工作区管理子菜单"
            @click.stop.prevent="toggleWorkspacesMenu"
            @keydown.enter.prevent="toggleWorkspacesMenu"
            @keydown.space.prevent="toggleWorkspacesMenu"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
          </span>
        </a>
      </RouterLink>
      <template v-if="workspacesExpanded">
        <RouterLink to="/collection-jobs" class="nav-sub" :class="{ active: collectionJobsActive }">
          数据采集作业
        </RouterLink>
        <RouterLink to="/publish-jobs" class="nav-sub" :class="{ active: publishJobsActive }">
          数据发布作业
        </RouterLink>
      </template>
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

/* 工作区管理子菜单折叠箭头：向下 = 收起，向上（旋转 180°）= 展开 */
.nav-chevron {
  margin-left: auto;
  width: 22px;
  height: 22px;
  display: grid;
  place-items: center;
  border-radius: 6px;
  color: #66788f;
  cursor: pointer;
  flex-shrink: 0;
}

.nav-chevron:hover {
  background: #e8f1ff;
  color: #2e7cd6;
}

.nav-chevron:focus-visible {
  outline: 2px solid #2e7cd6;
  outline-offset: 1px;
}

.nav-chevron svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform 0.15s ease;
}

.nav-chevron.open svg {
  transform: rotate(180deg);
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
