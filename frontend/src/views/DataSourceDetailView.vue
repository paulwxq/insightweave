<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { deleteDataSource, getDataSource, setDataSourceActivation } from '../api/dataSources'
import { displayDate } from '../api/format'
import { ApiError } from '../api/http'
import type { DataSourceListItem } from '../api/types'
import DeactivateSourceDialog from '../components/DeactivateSourceDialog.vue'
import DeleteSourceDialog from '../components/DeleteSourceDialog.vue'

const route = useRoute()
const router = useRouter()
const sourceId = computed(() =>
  typeof route.params.sourceId === 'string' ? route.params.sourceId : '',
)
const source = ref<DataSourceListItem | null>(null)
const loading = ref(true)
const loadError = ref('')
const confirmOpen = ref(false)
const deleteBusy = ref(false)
const deleteError = ref('')
const activationOpen = ref(false)
const activationBusy = ref(false)
const activationError = ref('')

function leaveMissing() {
  return router.replace({ path: '/data-sources', query: { notice: 'not_found' } })
}

async function load() {
  loading.value = true
  loadError.value = ''
  source.value = null
  confirmOpen.value = false
  activationOpen.value = false
  activationBusy.value = false
  activationError.value = ''
  try {
    source.value = await getDataSource(sourceId.value)
  } catch (error) {
    if (
      error instanceof ApiError &&
      (error.code === 'not_found' || error.code === 'invalid_request')
    ) {
      await leaveMissing()
      return
    }
    loadError.value = error instanceof Error ? error.message : '数据源读取失败。'
  } finally {
    loading.value = false
  }
}

function openDelete() {
  deleteBusy.value = false
  deleteError.value = ''
  confirmOpen.value = true
}

async function confirmDelete(deleteOutput: boolean) {
  if (deleteBusy.value || !source.value) return
  deleteBusy.value = true
  deleteError.value = ''
  try {
    await deleteDataSource(source.value.source_id, deleteOutput)
    await router.push('/data-sources')
  } catch (error) {
    if (error instanceof ApiError && error.code === 'not_found') {
      confirmOpen.value = false
      await leaveMissing()
      return
    }
    deleteError.value = error instanceof Error ? error.message : '删除失败。'
  } finally {
    deleteBusy.value = false
  }
}

function openActivation() {
  if (!source.value || activationBusy.value) return
  if (source.value.is_active) {
    activationBusy.value = false
    activationError.value = ''
    activationOpen.value = true
    return
  }
  void applyActivation(true)
}

async function applyActivation(isActive: boolean) {
  const current = source.value
  if (!current || activationBusy.value) return
  activationBusy.value = true
  activationError.value = ''
  try {
    source.value = await setDataSourceActivation(current.source_id, isActive)
    activationOpen.value = false
  } catch (error) {
    if (error instanceof ApiError && error.code === 'not_found') {
      activationOpen.value = false
      await leaveMissing()
      return
    }
    activationError.value = error instanceof Error ? error.message : '切换启用状态失败。'
  } finally {
    activationBusy.value = false
  }
}

watch(sourceId, () => void load(), { immediate: true })
</script>

<template>
  <div class="ds-detail">
    <RouterLink to="/data-sources" class="ds-back">← 返回数据源管理</RouterLink>
    <div v-if="loading" class="state-block" role="status">正在读取数据源…</div>
    <div v-else-if="loadError" class="state-block error-state" role="alert">
      <p>{{ loadError }}</p>
      <button type="button" class="button button-outline" @click="load">重试</button>
      <RouterLink to="/data-sources" class="button button-outline">返回数据源管理</RouterLink>
    </div>
    <template v-else-if="source">
      <header class="ds-detail-header">
        <div class="ds-title">
          <span class="ds-kicker">DATA SOURCE</span>
          <h1>{{ source.display_name }}</h1>
          <p class="ds-source-id">Source ID: {{ source.source_id }}</p>
          <p class="ds-lead">已登记的连接信息，只可查看；采集与发布作业在工作区内管理。</p>
        </div>
        <div class="ds-detail-actions">
          <button
            type="button"
            class="ds-enable"
            :disabled="activationBusy"
            :aria-pressed="source.is_active"
            :title="source.is_active ? '点击禁用该数据源' : '当前已禁用，点击启用'"
            @click="openActivation"
          >
            启用
            <span class="ds-switch" :class="{ off: !source.is_active }" aria-hidden="true"><span></span></span>
          </button>
          <p v-if="activationError && !activationOpen" class="status-message" role="alert">{{ activationError }}</p>
          <button type="button" class="ds-danger" @click="openDelete">删除</button>
          <RouterLink class="ds-edit" :to="`/data-sources/${source.source_id}/edit`">修改</RouterLink>
        </div>
      </header>

      <section class="ds-panel ds-panel-row">
        <div class="ds-panel-head">
          <span class="ds-step">1</span>
          <div>
            <h2>数据库类型</h2>
            <p>数据库类型不可修改</p>
          </div>
        </div>
        <span class="ds-type-badge">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <ellipse cx="12" cy="5" rx="9" ry="3" />
            <path d="M3 5v14a9 3 0 0 0 18 0V5" />
            <path d="M3 12a9 3 0 0 0 18 0" />
          </svg>
          PostgreSQL
        </span>
      </section>

      <section class="ds-panel">
        <div class="ds-panel-head">
          <span class="ds-step">2</span>
          <div>
            <h2>连接信息</h2>
            <p>以下为已保存的连接目标</p>
          </div>
        </div>
        <div class="ds-fields">
          <p><span>显示名称</span><strong>{{ source.display_name }}</strong></p>
          <p class="ds-mono"><span>主机</span><strong>{{ source.host }}</strong></p>
          <p class="ds-mono"><span>端口</span><strong>{{ source.port }}</strong></p>
          <p class="ds-mono"><span>数据库名</span><strong>{{ source.db_name }}</strong></p>
          <p class="ds-mono"><span>用户名</span><strong>{{ source.username }}</strong></p>
          <p class="ds-mono"><span>SSL 模式</span><strong>{{ source.sslmode || '未指定' }}</strong></p>
          <p class="ds-mono"><span>数据库版本</span><strong>{{ source.db_version || '版本未知' }}</strong></p>
          <p class="ds-mono"><span>登记时间</span><strong>{{ displayDate(source.created_at) }}</strong></p>
        </div>
      </section>

      <section class="ds-panel">
        <div class="ds-panel-head">
          <span class="ds-step">3</span>
          <div>
            <h2>已选 Schema</h2>
            <p>登记时保存的 Schema，打开本页不会重新连接</p>
          </div>
        </div>
        <div class="ds-schemas">
          <span v-for="schema in source.selected_schemas" :key="schema">{{ schema }}</span>
        </div>
      </section>

      <section class="ds-panel">
        <div class="ds-panel-head">
          <span class="ds-step">4</span>
          <div>
            <h2>说明</h2>
            <p>登记时填写的说明</p>
          </div>
        </div>
        <p class="ds-note">{{ source.description || '未填写' }}</p>
      </section>

      <section class="ds-panel">
        <div class="ds-panel-head">
          <span class="ds-step">5</span>
          <div>
            <h2>工作区</h2>
            <p>工作区隔离文件产物；同一数据源可有多个工作区，采集与发布作业都保存在工作区内</p>
          </div>
        </div>
        <div class="ws-entry-actions">
          <RouterLink class="ws-entry-link" :to="`/workspaces?source_id=${source.source_id}`">
            <strong>查看该数据源的工作区</strong>
            <small>进入工作区管理列表（按本数据源过滤）</small>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg>
          </RouterLink>
          <RouterLink class="ws-entry-link" :to="`/workspaces/new?source_id=${source.source_id}`">
            <strong>新建工作区</strong>
            <small>为本数据源创建新的工作区</small>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14" /><path d="M5 12h14" /></svg>
          </RouterLink>
        </div>
      </section>

      <div class="ds-job-links">
        <RouterLink :to="`/data-assets/${source.source_id}/objects`">JSON 对象画像 <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg></RouterLink>
      </div>
    </template>
    <DeactivateSourceDialog
      v-if="activationOpen && source"
      :display-name="source.display_name"
      :busy="activationBusy"
      :error="activationError"
      @cancel="activationOpen = false"
      @confirm="applyActivation(false)"
    />
    <DeleteSourceDialog
      v-if="confirmOpen && source"
      :display-name="source.display_name"
      :busy="deleteBusy"
      :error="deleteError"
      @cancel="confirmOpen = false"
      @confirm="confirmDelete"
    />
  </div>
</template>

<style scoped>
.ws-entry-actions {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
}
.ws-entry-link {
  display: flex;
  flex-direction: column;
  gap: 4px;
  border: 1px solid #e1e9f4;
  border-radius: 12px;
  background: #fcfdff;
  padding: 14px 18px;
  min-width: 240px;
  color: inherit;
}
.ws-entry-link:hover {
  border-color: #2e7cd6;
}
.ws-entry-link strong {
  color: #17293f;
  font-size: 14px;
}
.ws-entry-link small {
  color: #66788f;
  font-size: 11px;
}
.ws-entry-link svg {
  width: 14px;
  height: 14px;
  margin-top: 6px;
  align-self: flex-end;
  fill: none;
  stroke: #2e7cd6;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
@media (max-width: 800px) {
  .ws-entry-actions {
    flex-direction: column;
  }
}
</style>
