<script setup lang="ts">
import { computed, inject, ref } from 'vue'
import { useRoute } from 'vue-router'
import { notifyNewQueryReset } from '../modules/nl2sql/newQueryReset'
import { queryBusyKey } from '../modules/nl2sql/queryBusy'

const route = useRoute()
const queryBusy = inject(queryBusyKey, ref(false))
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

// doc 9 §4.3：展开状态拆成两个。强制展开由当前路由实时计算（不存储、不可收起），
// 手动展开是组件内布尔值（初始收起），实际展开 = 强制 或 手动。
const onGlobalJobsPage = computed(
  () => route.path === '/collection-jobs' || route.path === '/publish-jobs',
)
const manualExpanded = ref(false)
const menuOpen = computed(() => onGlobalJobsPage.value || manualExpanded.value)
function toggleWorkspacesMenu() {
  if (onGlobalJobsPage.value) return // 强制展开时点击箭头不改状态、不跳转（§4.3）
  manualExpanded.value = !manualExpanded.value
}
const queryActive = computed(() => route.path === '/query' || route.path.startsWith('/query/'))
const onNewQueryPage = computed(() => route.path === '/query/new')
const queryManualOpen = ref(false)
const queryMenuOpen = computed(() => onNewQueryPage.value || queryManualOpen.value)

function toggleQueryMenu() {
  if (onNewQueryPage.value) return
  queryManualOpen.value = !queryManualOpen.value
}

/**
 * doc 21 §5.3：普通左键、非忙碌且当前已在新建页时，即使 Vue Router 判定重复
 * 导航，也先同步通知当前页面清空选择与草稿，再继续一次无参数导航。
 * 修饰键／新标签页等情况只保留现有链接处理，不发送清空通知；不额外 replace。
 */
function guardNewQuery(event: MouseEvent, navigate: (event: MouseEvent) => void) {
  if (queryBusy.value) {
    event.preventDefault()
    return
  }
  if (
    !event.defaultPrevented &&
    (event.button === undefined || event.button === 0) &&
    !event.metaKey &&
    !event.ctrlKey &&
    !event.shiftKey &&
    !event.altKey &&
    onNewQueryPage.value
  ) {
    notifyNewQueryReset()
  }
  navigate(event)
}
</script>

<template>
  <aside class="sidebar">
    <div class="brand">
      <img class="brand-logo" src="/favicon.svg" width="34" height="34" alt="" />
      <div class="brand-text">
        <strong>MetaWeave</strong>
        <small>智能数据分析工作台</small>
      </div>
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
      <div class="nav-row" :class="{ active: queryActive }">
        <RouterLink to="/query">
          <span class="nav-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>
          </span>
          智能问数
        </RouterLink>
        <button
          type="button"
          class="nav-chevron"
          :class="{ open: queryMenuOpen }"
          :aria-expanded="queryMenuOpen"
          :aria-disabled="onNewQueryPage || undefined"
          aria-label="展开或收起智能问数子菜单"
          @click="toggleQueryMenu"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
        </button>
      </div>
      <template v-if="queryMenuOpen">
        <RouterLink to="/query/new" custom v-slot="{ href, navigate }">
          <a
            :href="href"
            class="nav-sub query-sub"
            :class="{ active: onNewQueryPage, 'is-busy': queryBusy }"
            :aria-disabled="queryBusy || undefined"
            @click="guardNewQuery($event, navigate)"
          >
            新建查询
          </a>
        </RouterLink>
        <span class="nav-disabled nav-sub query-sub" title="即将开放">新建分析 <small>即将开放</small></span>
      </template>
      <RouterLink to="/data-sources" custom v-slot="{ href, navigate }">
        <a :href="href" :class="{ active: sourcesActive }" @click="navigate">
          <span class="nav-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><rect width="20" height="8" x="2" y="2" rx="2" /><rect width="20" height="8" x="2" y="14" rx="2" /><path d="M6 6h.01" /><path d="M6 18h.01" /></svg>
          </span>
          数据源管理
        </a>
      </RouterLink>
      <RouterLink to="/workspaces" custom v-slot="{ href, navigate }">
        <div class="nav-row" :class="{ active: workspacesActive }">
          <a :href="href" @click="(event) => { navigate(event); manualExpanded = true }">
            <span class="nav-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24"><rect width="18" height="18" x="3" y="3" rx="2" /><path d="M3 9h18" /><path d="M9 21V9" /></svg>
            </span>
            工作区管理
          </a>
          <button
            type="button"
            class="nav-chevron"
            :class="{ open: menuOpen }"
            :aria-expanded="menuOpen"
            :aria-disabled="onGlobalJobsPage || undefined"
            aria-label="展开或收起工作区管理子菜单"
            @click="toggleWorkspacesMenu"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
          </button>
        </div>
      </RouterLink>
      <template v-if="menuOpen">
        <RouterLink to="/collection-jobs" class="nav-sub" :class="{ active: collectionJobsActive }">
          全部采集作业
        </RouterLink>
        <RouterLink to="/publish-jobs" class="nav-sub" :class="{ active: publishJobsActive }">
          全部发布作业
        </RouterLink>
      </template>
      <span class="nav-disabled">
        <span class="nav-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" /><circle cx="12" cy="12" r="3" /></svg>
        </span>
        系统设置 <small>待开放</small>
      </span>
    </nav>
  </aside>
</template>

<style scoped>
/* doc 9 §4.3 行容器承担选中背景与悬停样式；箭头是相邻 <button>，不嵌在 <a> 内 */
.nav-row {
  min-height: 44px;
  display: flex;
  align-items: center;
  gap: 4px;
  padding-right: 8px;
  border: 1px solid transparent;
  border-radius: 11px;
}

.nav-row.active {
  background: #eef3f9;
  border-color: #d9e3ef;
}

/* 行容器接管选中态后，文字/图标强调也需恢复（与其他入口选中视觉一致） */
.nav-row.active a {
  color: #17293f;
  font-weight: 700;
}

.nav-row.active .nav-icon {
  color: #2e7cd6;
}

.nav-row a {
  flex: 1;
  min-width: 0;
}

/* 工作区管理子菜单折叠箭头：向下 = 收起，向上（旋转 180°）= 展开 */
.nav-chevron {
  width: 22px;
  height: 22px;
  padding: 0;
  display: grid;
  place-items: center;
  border: 0;
  background: transparent;
  border-radius: 6px;
  color: #66788f;
  cursor: pointer;
  flex-shrink: 0;
}

.nav-chevron:hover {
  background: #e8f1ff;
  color: #2e7cd6;
}

.nav-chevron[aria-disabled='true'] {
  color: #9aa8bb;
  cursor: not-allowed;
}

.nav-chevron[aria-disabled='true']:hover {
  background: transparent;
  color: #9aa8bb;
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

@media (max-width: 800px) {
  /* 移动端侧边栏收窄为图标态，隐藏折叠箭头（沿用既有行为） */
  .nav-chevron,
  .query-sub {
    display: none !important;
  }
}

.nav-sub.is-busy {
  opacity: 0.45;
  cursor: not-allowed;
}

span.query-sub {
  padding-left: 36px;
  color: #98a6ba;
}

</style>
